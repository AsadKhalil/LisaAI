from abc import abstractmethod
import logging
from dotenv import load_dotenv
import os
import time
import traceback
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
import yaml
from langchain.agents import tool, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.agents.format_scratchpad.openai_tools import (
    format_to_openai_tool_messages,
)
from langchain.agents.output_parsers.openai_tools import OpenAIToolsAgentOutputParser
from app.src import constants
from sqlalchemy import create_engine
from app.src.modules.databases import (
    PGVectorManager,
    get_alchemy_conn_string,
    ConversationDB,
)

from app.src.constants import PROMPT, DATA
from redis import asyncio as aioredis
from langchain_aws import ChatBedrock
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

INPUT_KEY = "{input}"
load_dotenv()

class Agent:
    @abstractmethod
    async def _create_agent(self) -> None:
        pass

    @abstractmethod
    async def _build_prompt(self, language=None) -> None:
        pass

    @abstractmethod
    async def qa(self, query: str, history: list) -> str:
        pass


class LLMAgentFactory:
    """class for llm agent"""

    async def create(self) -> Agent:
        logger = logging.getLogger("LLMAgentFactory")

        REDIS_URL = os.environ.get("REDIS_URL")
        PROJECT_NAME = os.environ.get("PROJECT_NAME")
        # redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        # llm_id = await redis.get(
        #     f"{PROJECT_NAME}:llm_model",
        # )
        llm_id = os.environ.get("OPENAI_MODEL")
        logger.info("llm_id: %s", llm_id)
        resp = ""
        if llm_id == None:
            resp = "No LLM found"
            return resp
        #llm_id = "gpt-4o-mini"
        else:
            if llm_id in constants.OPENAI_MODELS:
                agent = OPENAIAgent()
                return agent
            elif llm_id in constants.BEDROCK_MODELS:
                agent = BedrockAgent()
                return agent
            else:
                resp = "LLM not in allowed list"
                return resp
        # agent = OPENAIAgent()
        # return agent
        


class OPENAIAgent(Agent):
    """class for function calling rag agent"""

    def __init__(self) -> None:
        self.logger = logging.getLogger("OPENAIAgent")
        self.conn_string = get_alchemy_conn_string()
        self.logger.info("connection string: %s", self.conn_string)
        self.engine = create_engine(self.conn_string)
        self.db = ConversationDB()
        self.user_id = None  # Will be set when agent is created

    def set_user_id(self, user_id: int):
        """Set the user ID for this agent instance"""
        self.user_id = user_id
        self.logger.info(f"User ID set for agent: {user_id}")

    async def _create_agent(self) -> None:
        self.llm = ChatOpenAI(model=self.llm_model_id, temperature=0.3)

        @tool
        async def semantic_search(search_term: str):
            """
            This function utilizes a vector store to retrieve relevant documents based on the semantic similarity of their content to the provided search term.
            """
            result = await self.db.get_active_files()
            VECTORSTORE_COLLECTION_NAME = os.environ.get("VECTORSTORE_COLLECTION_NAME")
            pgmanager = PGVectorManager()
            retriever = pgmanager.return_vector_store(
                VECTORSTORE_COLLECTION_NAME, async_mode=False
            )
            context = ""
            active_files = []
            print("#######################", result)
            for filename_tuple in result:

                filename = filename_tuple[0]
                # Extract the filename from the tuple
                active_files.append(filename)

            docs = retriever.similarity_search(
                search_term, k=5, filter={"source": active_files}
            )
            for doc in docs:
                content = doc.page_content
                context = context + content
            pgmanager.close()
            print("-------------------------------------------------------")
            print(context)
            return context

        @tool
        async def get_encounter_data():
            """
            Extract comprehensive encounter data for a patient. Use this when the user requests encounter summaries, 
            medical record summaries, or patient encounter information.
            
            IMPORTANT: User ID cannot be extracted from the prompt text. The response will only be related to the 
            user ID provided in the request parameters. Only use this function when the user is requesting their 
            own medical data or encounter summaries.
            """
            if self.user_id is None:
                return "Error: No user ID available to retrieve encounter data."
            
            from app.src.view import extract_encounter_data
            return extract_encounter_data(self.user_id)

        tools = [semantic_search, get_encounter_data]

        self.llm_with_tools = self.llm.bind_tools(tools)

        MEMORY_KEY = "chat_history"
        # self.logger.info("PROMPT: " + self.prompt)

        prompt = ChatPromptTemplate.from_messages(
            [
                # """
                # The user can ask about projects, technologies, industries or domains. Ask for clarification if you are unsure about the context of the question. Use semantic search if any other search fails.
                # """Use semantic search if anyother search fails.
                (
                    "system",
                    self.prompt,
                ),
                MessagesPlaceholder(variable_name=MEMORY_KEY),
                ("user", INPUT_KEY),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
            ]
        )

        agent = (
            {
                "input": lambda x: x["input"],
                "agent_scratchpad": lambda x: format_to_openai_tool_messages(
                    x["intermediate_steps"]
                ),
                "chat_history": lambda x: x["chat_history"],
            }
            | prompt
            | self.llm_with_tools
            | OpenAIToolsAgentOutputParser()
        )
        self.agent_executor = AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=True,
            return_intermediate_steps=True,
        )

    async def _build_prompt(self, language=None):
        self.logger.info(f"[DEBUG] _build_prompt called with language: {language}")
        self.llm_model_id = os.environ.get("OPENAI_MODEL")
        # Update prompt based on language
        if language == "en":
            self.prompt = PROMPT + "\n\n**CRITICAL INSTRUCTION**: From now on, respond only in English, regardless of previous conversation language. Do NOT respond in Spanish or any other language."
        else:
            self.prompt = PROMPT + "\n\n**INSTRUCCIÓN CRÍTICA**: Debes responder en español. No respondas en inglés ni en ningún otro idioma."
        self.logger.info(f"[DEBUG] Prompt set in _build_prompt: {self.prompt}")

    async def qa(self, query, chat_history):
        try:
            extracted_data = []

            for item in chat_history:

                # human = {"role": "human", "content": item["prompt"]}
                # extracted_data.append(human)
                # if item["response"] != None:
                #     assistant = {"role": "assistant", "content": item["response"]}
                if not isinstance(item, dict):
                    continue
                    
                prompt = item.get("prompt")
                if prompt:
                    human = {"role": "human", "content": prompt}
                    extracted_data.append(human)
                
                response = item.get("response")
                if response is not None:
                    assistant = {"role": "assistant", "content": response}
                    extracted_data.append(assistant)

            response = await self.agent_executor.ainvoke(
                {"input": query, "chat_history": extracted_data}
            )

            self.engine.dispose()

            result = response["output"]
            self.logger.critical("result: " + result)
            if response["intermediate_steps"]:
                context = ""
                for step in response["intermediate_steps"]:
                    if isinstance(step[-1], str):
                        context = context + ";" + step[-1]
                    elif isinstance(step[-1], dict) and len(step[-1]) > 1:
                        # Handle case where step[-1] is a dict with multiple items
                        context = context + ";" + str(step[-1])
                    else:
                        # Handle other cases safely
                        context = context + ";" + str(step[-1])

                return result, context

            return result, ""
        except Exception as e:
            self.logger.exception(traceback.format_exc())
            # Return a default response instead of None
            return "I apologize, but I encountered an error processing your request. Please try again.", ""


class BedrockAgent(Agent):
    def __init__(self):
        self.logger = logging.getLogger("BedrockAgent")
        super().__init__()

    async def _create_agent(self):
        self.agent = self.prompt | self.llm | StrOutputParser()

    async def _build_prompt(self, language=None):
        REDIS_URL = os.environ.get("REDIS_URL")
        PROJECT_NAME = os.environ.get("PROJECT_NAME")

        redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        model_id = await redis.get(
            f"{PROJECT_NAME}:llm_model",
        )

        if model_id in constants.BEDROCK_MODELS:
            self.llm = ChatBedrock(
                model_id=model_id,
                model_kwargs=dict(temperature=0),
                endpoint_url="https://bedrock-runtime.us-west-2.amazonaws.com",
                region_name="us-west-2",
            )

        persona = await redis.get(
            f"{PROJECT_NAME}:persona",
        )
        glossary = await redis.get(
            f"{PROJECT_NAME}:glossary",
        )
        tone = await redis.get(f"{PROJECT_NAME}:tone")
        response_length = await redis.get(f"{PROJECT_NAME}:response_length")
        content = await redis.get(f"{PROJECT_NAME}:content")
        
        # Add language instruction to the prompt
        language_instruction = ""
        if language == "en":
            language_instruction = " CRITICAL: You must respond in English."
        else:
            # Default to Spanish for any other language or no language specified
            language_instruction = " CRÍTICO: Debes responder en español. No respondas en inglés bajo ninguna circunstancia."
        
        PROMPT = r"""You are a {persona} and your job is to answer the user's questions.\
You can only answer questions using data provided as context.
Keep the length of the response {response_length}
the tone of the response should be {tone}
Here is the glossary for {glossary}
Here are some extra instructions:
{content}{language_instruction}

Provide a reference for every claim that you make\
If you cannot answer the question, just say "Sorry. I don't know."\
If the user provides specific instructions about response format, follow them.""".format(
            persona=persona,
            glossary=glossary,
            tone=tone,
            response_length=response_length,
            content=content,
            language_instruction=language_instruction,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", PROMPT),
                MessagesPlaceholder("chat_history"),
                ("human", "retrieved chunks: {context}"),
                ("human", INPUT_KEY),
            ]
        )
        self.prompt = prompt

        self.retrieverprompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    f"""You are a part of a vector store retriver. \
A Vector Store retriever calculates the cosine distance betweeen the embeddings of the input text and the stored embeddings, and it returns the closest embeddings. \
Given a user's prompt and chat history, formulate a single query that will be used to fetch relevant information. \
Return only the query and nothing else. Do not provide any explanation
You can use the following glossary to interpret the user's question:
{glossary}""",
                ),
                ("human", INPUT_KEY),
            ]
        )

    async def retriever_chain(self):
        manager = PGVectorManager()
        VECTORSTORE_COLLECTION_NAME = os.environ.get("VECTORSTORE_COLLECTION_NAME")
        retriever = manager.get_retriever(VECTORSTORE_COLLECTION_NAME, True)
        retrieverprompt = self.retrieverprompt
        retriever_query_chain = retrieverprompt | self.llm | StrOutputParser()

        retrieverchain = retriever | self.parse_retriever_output
        return retrieverchain, retriever_query_chain

    def parse_retriever_output(self, retriever_output):
        parsed_output = ""
        for document in retriever_output:
            parsed_output = parsed_output + document.page_content + "\n\n"
        return parsed_output

    async def qa(self, query, chat_history):
        total_start_time = time.time()
        retrieverchain, retriever_query_chain = await self.retriever_chain()
        retriever_query_creation_start_time = time.time()
        res = await retriever_query_chain.ainvoke(
            {"input": query, "chat_history": chat_history}
        )
        self.logger.info("res: %s", res)
        retriever_query_creation_end_time = time.time()
        self.logger.info(
            f"retriever query creation time: {retriever_query_creation_end_time - retriever_query_creation_start_time}"
        )
        context_start_time = time.time()
        context = await retrieverchain.ainvoke(res)
        context_end_time = time.time()
        context_time = context_end_time - context_start_time
        self.logger.info(f"context time: {context_time}")
        self.logger.info("context: %s", context)
        question = {"input": query, "chat_history": [], "context": context}
        response = await self.agent.ainvoke(question)

        total_response_time = time.time() - total_start_time
        self.logger.info(f"total response time: {total_response_time}")
        return response, context


# Simple direct OpenAI chat function (no RAG, no tools)
async def simple_openai_chat(prompt: str) -> str:
    """
    Sends a prompt to the OpenAI LLM and returns the response. No RAG, no tools, just a direct chat completion.
    """
    model = os.environ.get("OPENAI_MODEL_TREATMENT_GENERATION")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set.")
    llm = ChatOpenAI(model=model, openai_api_key=api_key, temperature=0.3)
    # LangChain's ChatOpenAI expects a list of messages
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    return response.content

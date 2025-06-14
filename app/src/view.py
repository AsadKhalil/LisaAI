import json
import logging
import sys
import traceback
from typing import Any, List
import app.src.constants as constants
from typing_extensions import Annotated
from fastapi import Depends, Response, UploadFile, HTTPException, APIRouter, File, Form
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer
# from firebase_admin.auth import UserRecord
from app.src.rating import add_rating_data
from app.src.data_types import ChangeRole, Conversation, Rating, Query, UpdateUser, Prompt, DeleteFile, ActiveFile
from .modules.databases import ConversationDB
from .modules.services import LLMAgentFactory
from .modules.auth import Authentication
from .modules.aws import AWS
from dotenv import load_dotenv
import time
import os
from app.src.knowledge_base import new_knowledge_base, create_drug_index, process_file
from redis import asyncio as aioredis
import app.src.error_messages as error_messages
from app.src.modules.databases import PGVectorManager
from pydantic import BaseModel

oauth2scheme = OAuth2PasswordBearer(
    tokenUrl="token",
)

load_dotenv()

router = APIRouter()

origins = [
    "*"
]

# Connect To Database
db = ConversationDB()
REDIS_URL = os.environ.get("REDIS_URL")
redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
logger = logging.getLogger("view(router)")


async def get_current_user(token: Annotated[str, Depends(oauth2scheme)]):
    """get current user"""
    try:
        auth = Authentication()
        user: UserRecord = await auth.authenticate_user(token)
        logger.info(f"Current user's firebase id: {user.uid}")
        logger.info(f"Current user's email: {user.email}")

        if user.custom_claims is not None:
            logger.info(
                f"Current user's local id: {user.custom_claims.get('local_id')}")
            logger.info(
                f"Current user's role: {user.custom_claims.get('role')}")
        else:
            logger.info("Current user's custom claims are None")
        if user.email_verified is False:
            raise HTTPException(status_code=401, detail="Email not verified")

        return user
    except Exception:
        traceback.print_exc()
        raise HTTPException(
            status_code=401, detail="Invalid authentication credentials")


@router.get("/")
async def get_home_page():
    """home page route"""
    PROJECT_NAME = os.environ.get("PROJECT_NAME")
    return f"Hello this is the {PROJECT_NAME} backend"


@router.post("/generate", response_class=HTMLResponse)
async def get_chatbot_response(query: Query):
    """route definition for chatbot"""
    try:
        start_time = time.time()
        logger.info(f"User's query: {query.input}")
        # user_id = current_user.uid
        # user_role = current_user.custom_claims.get('role')
        llm = await LLMAgentFactory().create()
        if type(llm) == str:
            return llm
        await llm._build_prompt()
        await llm._create_agent()

        # if current_user.custom_claims.get('local_id') is not None:
        #     user_id = current_user.custom_claims.get('local_id')
        #     logger.info(f"Current user's local id: {user_id}")
        user_id = 1
        conversation_id = None

        if not query.convo_id:  # Check if 'chat_history' is not present or empty
            conversation_id = await db.insert_conversation(
                user_id, query.input)
            logger.info(f"new Conversation ID: {conversation_id}")
        else:
            conversation_id = query.convo_id

        # If chat_history is not provided, fetch it from the database
        chat_history = query.chat_history
        if chat_history is None and conversation_id:
            conversation_rows = await db.get_conversation(conversation_id)
            chat_history = []
            for row in conversation_rows:
                chat_history.append({
                    "prompt": row[2],  # Question column
                    "response": row[3]  # Answer column
                })

        # chatbot's response
        response, context = await llm.qa(
            query.input, chat_history)
        end_time = time.time()
        response_time = end_time - start_time
        conversation_id = json.dumps(str(conversation_id))
        conversation_id = conversation_id.strip('"')
        # Store the query and response in the database
        query_id = await db.insert_query(conversation_id,
                                         query.input, response, context, response_time, user_id=user_id)
        query_id = json.dumps(str(query_id))
        query_id = query_id.strip('"')
        response = {"response": response,
                    "query_id": query_id, "convo_id": conversation_id}
        stringified_response = json.dumps(response)

        return stringified_response
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e)) from e

# @router.get("/check_drug_index")
# async def check_drug_index():
#     """Check if the drug index exists and has data"""
#     try:
#         pgmanager = PGVectorManager()
#         stats = pgmanager.get_collection_stats("drug-index")
#         pgmanager.close()
#         return stats
#     except Exception as e:
#         logger.error(f"Error checking drug index: {str(e)}")
#         logger.error(traceback.format_exc())
#         raise HTTPException(status_code=500, detail=str(e))

@router.post("/rating")
async def add_rating(data: Rating, current_user: Annotated[Any, Depends(get_current_user)]):
    """route for adding rating"""
    try:
        response = await add_rating_data(data, db)
        return response
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conversation")
async def get_conversation(data: Conversation):
    try:
        # This commented out code is for getting the previous conversation data from the database
        # response = await get_conversation_data(data, current_user, db)
        return Response(status_code=200)
    except AttributeError:
        logger.exception(traceback.format_exc())
        logger.exception(sys.exc_info()[2])
        raise HTTPException(
            status_code=401, detail="Unauthorised")
    except Exception:
        logger.exception(traceback.format_exc())
        logger.exception(sys.exc_info()[2])
        raise HTTPException(
            status_code=500, detail="Failed to get conversation")


@router.post("/change_role")
async def change_role_admin_endpoint(data: ChangeRole):
    try:
        auth = Authentication()
        user = await auth.get_user_by_email(data.email)
        if data.role not in [constants.ADMIN_ROLE, constants.DEFAULT_ROLE, constants.EMPLOYEE_ROLE]:
            raise HTTPException(
                status_code=400, detail=f"Role must be either {constants.ADMIN_ROLE} or {constants.DEFAULT_ROLE}")
        response1 = await auth.attach_role_to_user(user.uid, data.role)
        response2 = await db.change_user_role(data.role, data.email)
        response = {"firebase": response1, "database": response2}
        return response
    except HTTPException as http_exc:
        if http_exc.status_code == 400:
            raise http_exc
        else:
            logger.exception(traceback.format_exc())
            raise HTTPException(
                status_code=500, detail=error_messages.ROLE_CHANGE_FAILED)
    except Exception:
        logger.exception(traceback.format_exc())
        raise HTTPException(
            status_code=500, detail=error_messages.ROLE_CHANGE_FAILED)


@router.post("/fix_custom_claim")
async def change_role_admin_endpoint(data: ChangeRole):
    try:
        if data.role not in [constants.ADMIN_ROLE, constants.DEFAULT_ROLE, constants.EMPLOYEE_ROLE]:
            raise HTTPException(
                status_code=400, detail=f"Role must be either {constants.ADMIN_ROLE} or {constants.DEFAULT_ROLE}")
        auth = Authentication()
        user = await auth.get_user_by_email(data.email)
        user_from_db = await db.get_user_by_email(data.email)
        custom_claims = user.custom_claims
        if custom_claims is None:
            custom_claims = {}
            logger.critical("the user's Custom claims are None")

        if custom_claims.get('role') is None:
            custom_claims['role'] = data.role
            logger.critical("the user's role is None")

        if custom_claims.get('local_id') is None:
            print(user_from_db[0])
            custom_claims['local_id'] = str(user_from_db[0][0])
            logger.critical("the user's local_id was None")

        print(custom_claims)

        response1 = await auth.update_custom_claims(user.uid, custom_claims)
        response2 = await db.change_user_role(data.role, data.email)
        response = {"firebase": response1, "database": response2}
        return response
    except HTTPException as http_exc:
        if http_exc.status_code == 400:
            raise http_exc
        else:
            logger.exception(traceback.format_exc())
            raise HTTPException(
                status_code=500, detail=error_messages.ROLE_CHANGE_FAILED)
    except Exception:
        logger.exception(traceback.format_exc())
        raise HTTPException(
            status_code=500, detail=error_messages.ROLE_CHANGE_FAILED)


@router.get("/get_user_conversations")
async def get_user_conversations(current_user: Annotated[Any, Depends(get_current_user)]):
    try:
        user_id = current_user.uid
        if current_user.custom_claims.get('local_id') is not None:
            user_id = current_user.custom_claims.get('local_id')
        rows = await db.get_conversation_ids(user_id)

        response = []
        for row in rows:
            response.append({"convo_id": row[0], "title": row[1]})

        return response
    except Exception:
        logger.exception(traceback.format_exc())
        raise HTTPException(
            status_code=500, detail="Failed to get user conversations")


@router.get("/analysis_ask_engr")
async def get_analysis_ask_engr(current_user: Annotated[Any, Depends(get_current_user)]):
    if current_user.custom_claims.get('role') != 'Admin':
        raise HTTPException(status_code=401, detail="Unauthorised")
    else:
        try:
            response = await db.get_ask_engr_queries()
            return response
        except Exception as e:
            print(traceback.format_exc())
            print(sys.exc_info()[2])
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/analysis_ask_hr")
async def get_analysis_ask_hr(current_user: Annotated[Any, Depends(get_current_user)]):
    if current_user.custom_claims.get('role') != 'Admin':
        raise HTTPException(status_code=401, detail="Unauthorised")
    else:
        try:
            response = await db.get_ask_hr_queries()
            return response
        except Exception as e:
            print(traceback.format_exc())
            print(sys.exc_info()[2])
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/analysis_ask_engr_response_time")
async def get_analysis_ask_engr_response_time(current_user: Annotated[Any, Depends(get_current_user)]):
    if current_user.custom_claims.get('role') != 'Admin':
        raise HTTPException(status_code=401, detail="Unauthorised")
    else:
        try:
            response = await db.get_ask_engr_response_time()
            return response
        except Exception as e:
            print(traceback.format_exc())
            print(sys.exc_info()[2])
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/analysis_ask_hr_response_time")
async def get_analysis_ask_hr_response_time(current_user: Annotated[Any, Depends(get_current_user)]):
    if current_user.custom_claims.get('role') != 'Admin':
        raise HTTPException(status_code=401, detail="Unauthorised")
    else:
        try:
            response = await db.get_ask_hr_response_time()
            return response
        except Exception as e:
            print(traceback.format_exc())
            print(sys.exc_info()[2])
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/analysis_ask_engr_daily_usage")
async def get_analysis_ask_engr_daily_usage(current_user: Annotated[Any, Depends(get_current_user)]):
    if current_user.custom_claims.get('role') != 'Admin':
        raise HTTPException(status_code=401, detail="Unauthorised")
    else:
        try:
            response = await db.get_ask_engr_daily_usage()
            return response
        except Exception as e:
            print(traceback.format_exc())
            print(sys.exc_info()[2])
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/analysis_ask_hr_daily_usage")
async def get_analysis_ask_hr_daily_usage(current_user: Annotated[Any, Depends(get_current_user)]):
    if current_user.custom_claims.get('role') != 'Admin':
        raise HTTPException(status_code=401, detail="Unauthorised")
    else:
        try:
            response = await db.get_ask_hr_daily_usage()
            return response
        except Exception as e:
            print(traceback.format_exc())
            print(sys.exc_info()[2])
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/get_user_management_data")
async def get_user_management_data(current_user: Annotated[Any, Depends(get_current_user)]):
    try:
        if current_user.custom_claims.get('role') != 'Admin':
            raise HTTPException(status_code=401, detail="Unauthorised")
        else:
            response = await db.get_users()
            return response
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/update_user")
async def get_user_manager_data(current_user: Annotated[Any, Depends(get_current_user)], data: UpdateUser):
    try:
        email = current_user.email
        if email is None:
            raise HTTPException(status_code=400, detail="Bad Request")
        logger.info(f"time is {data.time}")
        response = await db.update_user(email, "last_session_duration", data.time)
        return response
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/change_user_role")
async def change_user_role_admin(current_user: Annotated[Any, Depends(get_current_user)], data: ChangeRole):
    try:
        if not current_user.custom_claims:
            raise HTTPException(
                status_code=400, detail="The Custom claim of user is none")
        role = current_user.custom_claims.get('role')
        if role != constants.ADMIN_ROLE:
            raise HTTPException(status_code=401, detail="Unauthorised")

        auth = Authentication()
        user = await auth.get_user_by_email(data.email)
        if data.role not in [constants.ADMIN_ROLE, constants.DEFAULT_ROLE, constants.EMPLOYEE_ROLE]:
            raise HTTPException(
                status_code=400, detail=f"Role must be either {constants.ADMIN_ROLE}, {constants.EMPLOYEE_ROLE} or {constants.DEFAULT_ROLE}")
        response1 = await auth.attach_role_to_user(user.uid, data.role)

        response = await db.change_user_role(data.role, data.email)

        return {"firebase": response1, "database": response}
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create_knowledge_base")
async def create_knowledge_base(files: Annotated[List[UploadFile], File()]):
    """route definition for creation of a new knowledge base with multiple file upload"""
    try:
        # if current_user.custom_claims.get('local_id') is not None:
        #     user_id = current_user.custom_claims.get('local_id')
        #     logger.info(f"Current user's local id: {user_id}")
        user_id = 1
        logger.info(type(files))
        logger.info(f"length of files {len(files)}")
        # return
        data = await new_knowledge_base(files=files)
        logger.info(f"Data being passed to add_files: {data}")
        _ = await db.add_files(data, user_id=user_id)
        return "Knowledge Base updated successfully"
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list-files")
async def list_files():
    try:
        # if current_user.custom_claims.get('role') != constants.ADMIN_ROLE:
        #     raise HTTPException(status_code=401, detail=" Unauthorised_")
        response = await db.get_files()
        json_list = []
        for tup in response:
            json_dict = {
                "filename": tup[0],
                "url": tup[1],
                "user_id": tup[2],
                "created_at": tup[3],
                "updated_at": tup[4],
                "active": tup[5],
            }
            json_list.append(json_dict)
        return json_list
    except AttributeError:
        logger.exception(traceback.format_exc())
        logger.exception(sys.exc_info()[2])
        raise HTTPException(
            status_code=401, detail="Unexpected Error")

# @router.get("/list-drug-index-files")
# async def list_drug_index_files():
#     """
#     Get a list of all files in the drug-index collection.
#     Returns file names and their status in the vector store.
#     """
#     try:
#         # Get file names from the drug-index collection
#         file_names = await db.get_file_names_by_collection("drug-index")
        
#         if not file_names:
#             return {"message": "No files found in drug-index collection.", "files": []}

#         # Get file details from the database
#         all_files = await db.get_files()
#         drug_index_files = []
        
#         # Filter and combine information
#         for file_name in file_names:
#             file_info = next((f for f in all_files if f[0] == file_name), None)
#             if file_info:
#                 drug_index_files.append({
#                     "filename": file_info[0],
#                     "url": file_info[1],
#                     "user_id": file_info[2],
#                     "created_at": file_info[3],
#                     "updated_at": file_info[4],
#                     "active": file_info[5]
#                 })

#         return {
#             "message": f"Found {len(drug_index_files)} files in drug-index collection",
#             "files": drug_index_files
#         }
#     except Exception as e:
#         logger.error(f"Error listing drug-index files: {str(e)}")
#         logger.error(traceback.format_exc())
#         raise HTTPException(status_code=500, detail=str(e))

@router.post('/delete-file')
async def delete_file(input: DeleteFile):
    try:
        aws = AWS()
        aws.delete_file(input.file_name)
        _ = await db.delete_file(input.file_name)
        _ = await db.delete_file_embeddings(input.file_name)
        return {"message": "File Delete Successfully", }
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

# @router.post("/clean-drug-index")
# async def clean_drug_index():
#     """
#     Deletes all files and embeddings from the drug-index collection.
#     Returns a summary of successful and failed deletions.
#     """
#     try:
#         # Step 1: Get all file names linked to the 'drug-index'
#         file_names = await db.get_file_names_by_collection("drug-index")
        
#         if not file_names:
#             return {"message": "No files found in drug-index collection."}

#         aws = AWS()
#         results = {
#             "successful": [],
#             "failed": []
#         }

#         for file_name in file_names:
#             try:
#                 # Delete from AWS
#                 aws.delete_file(file_name)
#                 # Delete from database
#                 await db.delete_file(file_name)
#                 # Delete embeddings
#                 await db.delete_file_embeddings(file_name)
#                 results["successful"].append(file_name)
#             except Exception as e:
#                 logger.error(f"Failed to delete file {file_name}: {str(e)}")
#                 results["failed"].append({"file": file_name, "error": str(e)})

#         return {
#             "message": f"Cleanup completed. {len(results['successful'])} files deleted successfully.",
#             "details": results
#         }
    
#     except Exception as e:
#         logger.error(f"Error in clean-drug-index: {str(e)}")
#         logger.error(traceback.format_exc())
#         raise HTTPException(status_code=500, detail=str(e))


@router.post("/file-active-toggle")
async def file_active_toggle(input: ActiveFile, current_user: Annotated[Any, Depends(get_current_user)]):
    """route for adding rating"""
    try:

        _ = await db.toggle_file_active(input.file_name, input.active)
        return {"message": "File Changed Successfully", }
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))


# current_user: Annotated[Any, Depends(get_current_user)]
@router.post("/prompts")
async def add_prompt(prompt: Prompt):
    """Endpoint for adding a new prompt."""
    try:
        PROJECT_NAME = os.environ.get("PROJECT_NAME")
        # Cache the prompt in Redis
        await redis.set(f"{PROJECT_NAME}:llm_model", prompt.llm_model)
        await redis.set(f"{PROJECT_NAME}:persona", prompt.persona)
        await redis.set(f"{PROJECT_NAME}:glossary", prompt.glossary)
        await redis.set(f"{PROJECT_NAME}:tone", prompt.tone)
        await redis.set(f"{PROJECT_NAME}:response_length", prompt.response_length)
        await redis.set(f"{PROJECT_NAME}:content", prompt.content)

        response = await db.insert_prompt(prompt)
        id_json = json.dumps(str(response))
        id_json = id_json.strip('"')
        return {"id": id_json, "content": prompt.content}
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prompts")
async def get_prompt(current_user: Annotated[Any, Depends(get_current_user)]):
    if current_user.custom_claims.get('role') != 'Admin':
        raise HTTPException(status_code=401, detail="Unauthorised")
    else:
        try:
            response = await db.get_prompt()
            return {
                "llm_model": response[0],
                "persona": response[1],
                "glossary": response[2],
                "tone": response[3],
                "response_length": response[4],
                "content": response[5]
            }
        except Exception as e:
            print(traceback.format_exc())
            print(sys.exc_info()[2])
            raise HTTPException(status_code=500, detail=str(e))


# @router.post("/drug_index")
# async def drug_index_endpoint(files: Annotated[List[UploadFile], File()]):
#     try:
#         logger.info(f"Received {len(files)} files for drug index processing")
#         results = []
#         for file in files:
#             logger.info(f"Processing file: {file.filename}")
#             result = await process_file(file, collection_name="drug-index")
#             logger.info(f"Completed processing file: {file.filename}")
#             results.append(result)
#         logger.info("All files processed successfully")
#         return {"status": "Drug Index updated successfully", "files": results}
#     except Exception as e:
#         logger.error(f"Error in drug_index endpoint: {str(e)}")
#         logger.error(traceback.format_exc())
#         raise HTTPException(status_code=500, detail=str(e))


class DrugQuery(BaseModel):
    query: str

# @router.post("/query_drug")
# async def query_drug_endpoint(query: DrugQuery):
#     """Query the drug index for specific drug information"""
#     try:
#         logger.info(f"Starting drug query endpoint with query: {query.query}")
#         VECTORSTORE_COLLECTION_NAME = "drug-index"
#         logger.info(f"Using vector store collection: {VECTORSTORE_COLLECTION_NAME}")

#         pgmanager = PGVectorManager()
#         logger.info("Initialized PGVectorManager")

#         # Set up retriever (we filter manually)
#         retriever = pgmanager.get_retriever(
#             VECTORSTORE_COLLECTION_NAME,
#             async_mode=False,
#             search_kwargs={'k': 5}
#         )
#         logger.info("Retriever initialized without score_threshold")

#         # Perform similarity search with scores
#         logger.info("Performing similarity_search_with_score")
#         results = retriever.vectorstore.similarity_search_with_score(query.query, k=5)

#         score_threshold = 0.75  # Lowered threshold to catch more relevant docs
#         filtered_docs = []

#         for doc, score in results:
#             snippet = doc.page_content.strip()[:120].replace("\n", " ")
#             logger.info(f"Score: {score:.4f} | Snippet: {snippet}")
#             if score >= score_threshold:
#                 filtered_docs.append(doc)

#         if not filtered_docs:
#             logger.warning("No documents found above threshold")
#             return {"response": "I don't have information about that in my database. Please try asking about a different medical condition or drug."}

#         # Use best matching document
#         best_match_content = filtered_docs[0].page_content.strip()
#         logger.info("Returning best match document content")

#         return {"response": best_match_content}

#     except Exception as e:
#         logger.error(f"Error in query_drug endpoint: {str(e)}")
#         logger.error(traceback.format_exc())
#         raise HTTPException(status_code=500, detail="Internal server error")



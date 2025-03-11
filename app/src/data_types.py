from typing import Union
from pydantic import BaseModel
import uuid


class Rating(BaseModel):
    rating: Union[int, None]
    query_id: Union[str, None]
    review: str


class Prompt(BaseModel):
    llm_model: str
    persona: str
    glossary: str
    tone: str
    response_length: str
    content: str


class Query(BaseModel):
    """
    chatbot query input
    """
    input: str
    chat_history: list
    convo_id: Union[str, None]


class SmartSearchInput(BaseModel):
    """
    Smart search input
    """
    filter: str
    search: str


class PptInput(BaseModel):
    """
    Ppt input
    """
    vertical: str
    domains: list
    desc: str


class CaseStudyMakerInput(BaseModel):
    """
    Case Study Maker input
    """
    client: str


class Analysis(BaseModel):
    """analysis data"""
    number_of_queries: int


class SignUp(BaseModel):
    email: str
    name: str
    password: str
    designation: str
    department: str


class Login(BaseModel):
    email: str
    password: str


class Conversation(BaseModel):
    conversation_id: str


class EmailInput(BaseModel):
    thread_of_emails: list


class EmailInputUserData(BaseModel):
    userInput: str
    role: str
    tone: str
    emailLength: str
    thread_of_emails: list


class GoogleSignup(BaseModel):
    email: str
    name: str
    uid: str


class ChangeRole(BaseModel):
    email: str
    role: str


class HRBotInput(BaseModel):
    input: str
    chat_history: list
    convo_id: Union[str, None]


class IngestFile(BaseModel):
    key: str
    data_type: str
    bucket_name: str
    event_type: str


class Delete(BaseModel):
    type: str


class DeleteFile(BaseModel):
    file_name: str


class ActiveFile(BaseModel):
    file_name: str
    active: bool


class Template(BaseModel):
    template_name: str
    attributes: list


class HRRefrenceInput(BaseModel):
    input: str


class UpdateUser(BaseModel):
    email: Union[str, None]
    name: Union[str, None]
    designation: Union[str, None]
    department: Union[str, None]
    role: Union[str, None]
    time: Union[str, None]

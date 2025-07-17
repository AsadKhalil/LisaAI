import json
import re
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
from app.src.data_types import (
    ChangeRole,
    Conversation,
    Rating,
    Query,
    UpdateUser,
    Prompt,
    DeleteFile,
    ActiveFile,
    TreatmentPlanRequest,
)
from .modules.databases import ConversationDB
from .modules.services import LLMAgentFactory, simple_openai_chat
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
import mysql.connector
from datetime import datetime, date
import subprocess
import tempfile
from fastapi.responses import FileResponse, Response
from io import BytesIO
from jinja2 import Template, Environment
import random
import string

oauth2scheme = OAuth2PasswordBearer(
    tokenUrl="token",
)

load_dotenv()

router = APIRouter()

origins = ["*"]

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
                f"Current user's local id: {user.custom_claims.get('local_id')}"
            )
            logger.info(f"Current user's role: {user.custom_claims.get('role')}")
        else:
            logger.info("Current user's custom claims are None")
        if user.email_verified is False:
            raise HTTPException(status_code=401, detail="Email not verified")

        return user
    except Exception:
        traceback.print_exc()
        raise HTTPException(
            status_code=401, detail="Invalid authentication credentials"
        )


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
            conversation_id = await db.insert_conversation(user_id, query.input)
            logger.info(f"new Conversation ID: {conversation_id}")
        else:
            conversation_id = query.convo_id

        # If chat_history is not provided, fetch it from the database
        chat_history = query.chat_history
        if chat_history is None and conversation_id:
            conversation_rows = await db.get_conversation(conversation_id)
            chat_history = []
            for row in conversation_rows:
                chat_history.append(
                    {
                        "prompt": row[2],  # Question column
                        "response": row[3],  # Answer column
                    }
                )

        # chatbot's response
        response, context = await llm.qa(query.input, chat_history)
        end_time = time.time()
        response_time = end_time - start_time
        conversation_id = json.dumps(str(conversation_id))
        conversation_id = conversation_id.strip('"')
        # Store the query and response in the database
        query_id = await db.insert_query(
            conversation_id,
            query.input,
            response,
            context,
            response_time,
            user_id=user_id,
        )
        query_id = json.dumps(str(query_id))
        query_id = query_id.strip('"')
        response = {
            "response": response,
            "query_id": query_id,
            "convo_id": conversation_id,
        }
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
async def add_rating(
    data: Rating, current_user: Annotated[Any, Depends(get_current_user)]
):
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
        raise HTTPException(status_code=401, detail="Unauthorised")
    except Exception:
        logger.exception(traceback.format_exc())
        logger.exception(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail="Failed to get conversation")


@router.post("/change_role")
async def change_role_admin_endpoint(data: ChangeRole):
    try:
        auth = Authentication()
        user = await auth.get_user_by_email(data.email)
        if data.role not in [
            constants.ADMIN_ROLE,
            constants.DEFAULT_ROLE,
            constants.EMPLOYEE_ROLE,
        ]:
            raise HTTPException(
                status_code=400,
                detail=f"Role must be either {constants.ADMIN_ROLE} or {constants.DEFAULT_ROLE}",
            )
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
                status_code=500, detail=error_messages.ROLE_CHANGE_FAILED
            )
    except Exception:
        logger.exception(traceback.format_exc())
        raise HTTPException(status_code=500, detail=error_messages.ROLE_CHANGE_FAILED)


@router.post("/fix_custom_claim")
async def change_role_admin_endpoint(data: ChangeRole):
    try:
        if data.role not in [
            constants.ADMIN_ROLE,
            constants.DEFAULT_ROLE,
            constants.EMPLOYEE_ROLE,
        ]:
            raise HTTPException(
                status_code=400,
                detail=f"Role must be either {constants.ADMIN_ROLE} or {constants.DEFAULT_ROLE}",
            )
        auth = Authentication()
        user = await auth.get_user_by_email(data.email)
        user_from_db = await db.get_user_by_email(data.email)
        custom_claims = user.custom_claims
        if custom_claims is None:
            custom_claims = {}
            logger.critical("the user's Custom claims are None")

        if custom_claims.get("role") is None:
            custom_claims["role"] = data.role
            logger.critical("the user's role is None")

        if custom_claims.get("local_id") is None:
            print(user_from_db[0])
            custom_claims["local_id"] = str(user_from_db[0][0])
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
                status_code=500, detail=error_messages.ROLE_CHANGE_FAILED
            )
    except Exception:
        logger.exception(traceback.format_exc())
        raise HTTPException(status_code=500, detail=error_messages.ROLE_CHANGE_FAILED)


@router.get("/get_user_conversations")
async def get_user_conversations(
    current_user: Annotated[Any, Depends(get_current_user)],
):
    try:
        user_id = current_user.uid
        if current_user.custom_claims.get("local_id") is not None:
            user_id = current_user.custom_claims.get("local_id")
        rows = await db.get_conversation_ids(user_id)

        response = []
        for row in rows:
            response.append({"convo_id": row[0], "title": row[1]})

        return response
    except Exception:
        logger.exception(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get user conversations")


@router.get("/analysis_ask_engr")
async def get_analysis_ask_engr(
    current_user: Annotated[Any, Depends(get_current_user)],
):
    if current_user.custom_claims.get("role") != "Admin":
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
    if current_user.custom_claims.get("role") != "Admin":
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
async def get_analysis_ask_engr_response_time(
    current_user: Annotated[Any, Depends(get_current_user)],
):
    if current_user.custom_claims.get("role") != "Admin":
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
async def get_analysis_ask_hr_response_time(
    current_user: Annotated[Any, Depends(get_current_user)],
):
    if current_user.custom_claims.get("role") != "Admin":
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
async def get_analysis_ask_engr_daily_usage(
    current_user: Annotated[Any, Depends(get_current_user)],
):
    if current_user.custom_claims.get("role") != "Admin":
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
async def get_analysis_ask_hr_daily_usage(
    current_user: Annotated[Any, Depends(get_current_user)],
):
    if current_user.custom_claims.get("role") != "Admin":
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
async def get_user_management_data(
    current_user: Annotated[Any, Depends(get_current_user)],
):
    try:
        if current_user.custom_claims.get("role") != "Admin":
            raise HTTPException(status_code=401, detail="Unauthorised")
        else:
            response = await db.get_users()
            return response
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/update_user")
async def get_user_manager_data(
    current_user: Annotated[Any, Depends(get_current_user)], data: UpdateUser
):
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
async def change_user_role_admin(
    current_user: Annotated[Any, Depends(get_current_user)], data: ChangeRole
):
    try:
        if not current_user.custom_claims:
            raise HTTPException(
                status_code=400, detail="The Custom claim of user is none"
            )
        role = current_user.custom_claims.get("role")
        if role != constants.ADMIN_ROLE:
            raise HTTPException(status_code=401, detail="Unauthorised")

        auth = Authentication()
        user = await auth.get_user_by_email(data.email)
        if data.role not in [
            constants.ADMIN_ROLE,
            constants.DEFAULT_ROLE,
            constants.EMPLOYEE_ROLE,
        ]:
            raise HTTPException(
                status_code=400,
                detail=f"Role must be either {constants.ADMIN_ROLE}, {constants.EMPLOYEE_ROLE} or {constants.DEFAULT_ROLE}",
            )
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
        raise HTTPException(status_code=401, detail="Unexpected Error")


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


@router.post("/delete-file")
async def delete_file(input: DeleteFile):
    try:
        aws = AWS()
        aws.delete_file(input.file_name)
        _ = await db.delete_file(input.file_name)
        _ = await db.delete_file_embeddings(input.file_name)
        return {
            "message": "File Delete Successfully",
        }
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
async def file_active_toggle(
    input: ActiveFile, current_user: Annotated[Any, Depends(get_current_user)]
):
    """route for adding rating"""
    try:

        _ = await db.toggle_file_active(input.file_name, input.active)
        return {
            "message": "File Changed Successfully",
        }
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
    if current_user.custom_claims.get("role") != "Admin":
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
                "content": response[5],
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


def convert_datetimes(obj):
    """Convert datetime objects to strings for JSON serialization"""
    if isinstance(obj, dict):
        return {k: convert_datetimes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_datetimes(i) for i in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, date):
        return obj.isoformat()
    else:
        return obj


def filter_patient_data(
    patient,
    allergies,
    problems,
    medications,
    vitals,
    laboratory,
    family_history,
    doctor_name=None,
    doctor_id=None,
):
    # Ensure both 'firstName' and 'name' are supported for patient name
    patient_name = patient.get("firstName")
    filtered = {
        "patient": {
            "name": patient_name,
            "id": patient.get("id"),
            "dob": "",
            "gender": (
                "Male"
                if patient.get("sexAtBirthCode") == "gender_at_birth_male"
                else (
                    "Female"
                    if patient.get("sexAtBirthCode") == "gender_at_birth_female"
                    else "other"
                )
            ),
        },
        "doctor": {"name": doctor_name, "id": doctor_id, "signature": ""},
        "problems": (
            [{"description": p.get("problemorissue")} for p in problems]
            if problems
            else [{"description": None}]
        ),
        "allergies": (
            [
                {
                    "name": a.get("allergy"),
                    "type": a.get("allergytype"),
                    "severity": a.get("severitiesCode"),
                }
                for a in allergies
            ]
            if allergies
            else [{"name": None, "type": None, "severity": None}]
        ),
        "vitals": (
            [
                {
                    "height": (
                        v.get("heightFt") + "ft" + v.get("heightIn") + "in"
                        if v.get("heightFt") and v.get("heightIn")
                        else None
                    ),
                    "weight": (
                        v.get("weightKilo")
                        + "."
                        + v.get("weightGram")
                        + v.get("weightUnit")
                        if v.get("weightKilo")
                        and v.get("weightGram")
                        and v.get("weightUnit")
                        else None
                    ),
                    "bmi": v.get("bmi"),
                    "heart_rate": v.get("pulseBpm"),
                    "blood_pressure": (
                        v.get("systolicBloodPressure")
                        + "/"
                        + v.get("diastolicBloodPressure")
                        if v.get("systolicBloodPressure")
                        and v.get("diastolicBloodPressure")
                        else None
                    ),
                    "date": v.get("recordDate"),
                }
                for v in vitals
            ]
            if vitals
            else [
                {
                    "height": None,
                    "weight": None,
                    "bmi": None,
                    "heart_rate": None,
                    "blood_pressure": None,
                    "date": None,
                }
            ]
        ),
        "medications": (
            [
                {
                    "name": m.get("drugname"),
                    "qty": m.get("quantity"),
                    "dosage": m.get("dose"),
                    "reason": m.get("reason"),
                    "instruction": m.get("instruction"),
                }
                for m in medications
            ]
            if medications
            else [
                {
                    "name": None,
                    "qty": None,
                    "dosage": None,
                    "reason": None,
                    "instruction": None,
                }
            ]
        ),
    }

    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items() if v not in [None, "", [], {}]}
        elif isinstance(obj, list):
            return [clean(i) for i in obj if i not in [None, "", [], {}]]
        else:
            return obj

    return clean(filtered)


@router.post("/treatment-plan")
async def generate_treatment_plan(request: TreatmentPlanRequest):

    try:
        t0 = time.time()
        global mysql_connection
        if mysql_connection is None or not mysql_connection.is_connected():
            init_mysql_connection()
        connection = mysql_connection
        patient_id = request.patient_id
        doctor_name = request.doctor_name
        doctor_id = request.doctor_id
        organization_id = request.organization_id
        reference_number = request.reference_number
        cursor = connection.cursor(dictionary=True)

        cursor.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
        patient = cursor.fetchone()

        cursor.execute("SELECT * FROM allergies WHERE patientId = %s", (patient_id,))
        allergies = cursor.fetchall()

        cursor.execute("SELECT * FROM problem WHERE patient_id = %s", (patient_id,))
        problems = cursor.fetchall()

        cursor.execute(
            "SELECT * FROM patient_medications WHERE patient_id = %s", (patient_id,)
        )
        medications = cursor.fetchall()

        cursor.execute("SELECT * FROM vitals WHERE patientId = %s", (patient_id,))
        vitals = cursor.fetchall()

        cursor.execute("SELECT * FROM laboratory WHERE patientId = %s", (patient_id,))
        laboratory = cursor.fetchall()

        cursor.execute(
            "SELECT * FROM family_history WHERE patientId = %s", (patient_id,)
        )
        family_history = cursor.fetchall()

        cursor.close()

        t1 = time.time()
        logger.info(f"MySQL fetch took {t1-t0:.2f}s")

        # Prepare filtered data for template
        filtered_data = filter_patient_data(
            patient,
            allergies,
            problems,
            medications,
            vitals,
            laboratory,
            family_history,
            doctor_name=doctor_name,
            doctor_id=doctor_id,
        )
        patient_data_serializable = convert_datetimes(
            filtered_data
        )  # This line now has purpose with datetime conversion placeholder

        # Debug log patient data before rendering
        logger.info(f"Patient data for template: {filtered_data['patient']}")
        logger.info(f"allergies data for template: {filtered_data['allergies']}")
        logger.info(f"doctor data for template: {filtered_data['doctor']}")
        logger.info(f"problem data for template: {filtered_data['problems']}")
        logger.info(f"vitals data for template: {filtered_data['vitals']}")
        logger.info(f"medications data for template: {filtered_data['medications']}")

        latex_template = r"""
\documentclass[10pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[margin=0.75in]{geometry}
\usepackage[table]{xcolor}
\usepackage{array}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage{tcolorbox}
\newcolumntype{L}[1]{>{\raggedright\arraybackslash}p{#1}}
\definecolor{lightgray}{gray}{0.9}
\begin{document}

\begin{center}
\Large\textbf{[[ vars.treatment_plan ]]} \\[6pt]
\small\textbf{[[ vars.reference ]] \:} \texttt{[[ reference_number ]]}
\end{center}

\noindent
\makebox[\textwidth]{
\begin{tabular}{|>{\columncolor{lightgray}}L{0.18\textwidth}|L{0.22\textwidth}|>{\columncolor{lightgray}}L{0.18\textwidth}|L{0.22\textwidth}|>{\columncolor{lightgray}}L{0.18\textwidth}|L{0.22\textwidth}|}
\hline
\textbf{[[ vars.patient_name ]]} & [[ patient.name ]] & \textbf{[[ vars.patient_id ]]} & [[ patient.id ]] \\
\hline
\textbf{[[ vars.dob ]]} & [[ patient.dob ]] & \textbf{[[ vars.gender ]]} & [[ patient.gender ]] \\
\hline
\textbf{[[ vars.doctor_name ]]} & [[ doctor.name ]] & \textbf{[[ vars.doctor_id ]]} & [[ doctor.id ]] \\
\hline
\textbf{[[ vars.signature ]]} & [[ doctor.signature ]] & \textbf{[[ vars.organization_id ]]} & [[ organization_id ]] \\
\hline
\end{tabular}
}

\vspace{24pt}

\section*{[[ vars.problem_list ]]}
\begin{enumerate}[label=\arabic*.]
[% for p in problems %]
  \item [[ p.description ]]
[% endfor %]
\end{enumerate}

\vspace{24pt}

\section*{[[ vars.allergies ]]}
\makebox[\textwidth]{
\begin{tabular}{|>{\columncolor{lightgray}}L{0.33\textwidth}|>{\columncolor{lightgray}}L{0.33\textwidth}|>{\columncolor{lightgray}}L{0.33\textwidth}|}
\hline
\textbf{[[ vars.allergy_name ]]} & \textbf{[[ vars.allergy_type ]]} & \textbf{[[ vars.severity_level ]]} \\
\hline
[% for a in allergies %]
[[ a.name ]] & [[ a.type ]] & [[ a.severity ]] \\
\hline
[% endfor %]
\end{tabular}
}

\vspace{24pt}

\section*{[[ vars.vitals ]]}
\makebox[\textwidth]{
\begin{tabular}{|>{\columncolor{lightgray}}L{0.19\textwidth}|>{\columncolor{lightgray}}L{0.19\textwidth}|>{\columncolor{lightgray}}L{0.19\textwidth}|>{\columncolor{lightgray}}L{0.19\textwidth}|>{\columncolor{lightgray}}L{0.19\textwidth}|}
\hline
\textbf{[[ vars.height ]]} & \textbf{[[ vars.weight ]]} & \textbf{[[ vars.bmi ]]} & \textbf{[[ vars.heart_rate ]]} & \textbf{[[ vars.blood_pressure ]]} \\
\hline
[% for v in vitals %]
[[ v.height ]] & [[ v.weight ]] & [[ v.bmi ]] & [[ v.heart_rate ]] & [[ v.blood_pressure ]] \\
\hline
[% endfor %]
\end{tabular}
}
\vspace{4pt}
[% if vitals %]
{\footnotesize \textit{[[ vars.vitals ]] recorded on: [[ vitals[0].date ]] }}
[% endif %]

\vspace{24pt}

\section*{[[ vars.active_medications ]]}
\makebox[\textwidth]{
\begin{tabular}{|>{\columncolor{lightgray}}L{0.2\textwidth}|>{\columncolor{lightgray}}L{0.12\textwidth}|>{\columncolor{lightgray}}L{0.2\textwidth}|>{\columncolor{lightgray}}L{0.2\textwidth}|>{\columncolor{lightgray}}L{0.28\textwidth}|}
\hline
\textbf{[[ vars.medication_name ]]} & \textbf{[[ vars.qty ]]} & \textbf{[[ vars.dosage ]]} & \textbf{[[ vars.reason ]]} & \textbf{[[ vars.instruction ]]} \\
\hline
[% for m in medications %]
[[ m.name ]] & [[ m.qty ]] & [[ m.dosage ]] & [[ m.reason ]] & [[ m.instruction ]] \\
\hline
[% endfor %]
\end{tabular}
}

\vspace{24pt}

\section*{[[ vars.assessment_plan ]]}
\begin{itemize}
[% for step in assessment_steps %]
  \item [[ step.step_description ]][% if step.timeline %] --- [[ step.timeline ]][% endif %]
[% endfor %]
\end{itemize}

\end{document}
"""

        # Use a safe environment to avoid conflicts with LaTeX
        env = Environment(
            block_start_string="[%",
            block_end_string="%]",
            variable_start_string="[[",
            variable_end_string="]]",
            # ADD THESE TWO LINES TO DEFINE JINJA2 COMMENT DELIMITERS
            comment_start_string="<#",  # Unlikely to appear in LaTeX
            comment_end_string="#>",  # Unlikely to appear in LaTeX
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.from_string(latex_template)
        language = request.language if hasattr(request, "language") else "eng"
        logger.info(f"Language for treatment plan: {language}")
        # Get the LLM model for treatment generation from .env
        OPENAI_MODEL_TREATMENT_GENERATION = os.environ.get("OPENAI_MODEL_TREATMENT_GENERATION")
        # Call LLM only for assessment steps
        # Pass the model name to simple_openai_chat
        patient_name = filtered_data["patient"]["name"]
        problems_list = [p.get("description") for p in filtered_data["problems"]]
        allergies_list = [a.get("name") for a in filtered_data["allergies"]]
        medications_list = [m.get("name") for m in filtered_data["medications"]]
        summary = f"Patient: {patient_name}, Problems: {problems_list}, Allergies: {allergies_list}, Medications: {medications_list}"
        assessment_prompt = (
            "Given the following patient summary, generate an assessment plan as a JSON array (7-10 steps, each with 'step_description' and 'timeline'). "
            "No explanations, no markdown, just valid JSON. if the lamguage is 'esp', then your response should be in spanish. \n\n"
            " ***DO NOT Hullucinate if the give response in the requested language *** "
            f"PATIENT SUMMARY: {summary}"
            f"LANGUAGE: {language}"
        )
        t2 = time.time()
        llm_response = await simple_openai_chat(assessment_prompt, model=OPENAI_MODEL_TREATMENT_GENERATION)
        t3 = time.time()
        logger.info(f"LLM call took {t3-t2:.2f}s")
        logger.info(f"LLM response size: {len(llm_response)} chars")

        t4 = time.time()
        try:
            json_match = re.search(r"\[.*\]", llm_response, re.DOTALL)
            if json_match:
                assessment_steps = json.loads(json_match.group())
            else:
                raise HTTPException(
                    status_code=500, detail="LLM did not return valid JSON array."
                )
        except Exception as e:
            logger.error(f"Failed to parse LLM JSON response: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to parse LLM JSON response: {e}"
            )
        t5 = time.time()
        logger.info(f"LLM response parsing took {t5-t4:.2f}s")

        # Remove wrap_latex_safe and use assessment_steps directly
        # if isinstance(assessment_steps, list):
        #     for step in assessment_steps:
        #         if 'step_description' in step and step['step_description']:
        #             step['step_description'] = wrap_latex_safe(step['step_description'])
        #         if 'timeline' in step and step['timeline']:
        #             step['timeline'] = wrap_latex_safe(step['timeline'])

        # Before rendering, replace underscores with spaces in all template data
        patient = replace_underscores(filtered_data["patient"])
        doctor = replace_underscores(filtered_data["doctor"])
        problems = replace_underscores(filtered_data["problems"])
        allergies = replace_underscores(filtered_data["allergies"])
        vitals = replace_underscores(filtered_data["vitals"])
        medications = replace_underscores(filtered_data["medications"])
        org_id_render = (
            replace_underscores(organization_id) if organization_id else None
        )
        ref_num_render = (
            replace_underscores(reference_number) if reference_number else None
        )

        # Async batch translation using LLM
        async def llm_batch_translate(obj, target_lang="es"):
            strings = []

            def collect_strings(o):
                if isinstance(o, dict):
                    for v in o.values():
                        collect_strings(v)
                elif isinstance(o, list):
                    for i in o:
                        collect_strings(i)
                elif isinstance(o, str):
                    strings.append(o)

            collect_strings(obj)
            if not strings:
                return obj
            prompt = (
                f"Translate the following list of English phrases to {target_lang}. "
                "Return a JSON array of translations:"
                f"{json.dumps(strings)}"
            )
            response = await simple_openai_chat(prompt, model=OPENAI_MODEL_TREATMENT_GENERATION)
            match = re.search(r"\[.*\]", response, re.DOTALL)
            if match:
                translated = json.loads(match.group())
            else:
                raise Exception("LLM did not return valid JSON array")
            it = iter(translated)

            def reconstruct(o):
                if isinstance(o, dict):
                    return {k: reconstruct(v) for k, v in o.items()}
                elif isinstance(o, list):
                    return [reconstruct(i) for i in o]
                elif isinstance(o, str):
                    return next(it)
                else:
                    return o

            return reconstruct(obj)

        # Translate all data to Spanish if requested (batch, LLM)
        if getattr(request, "language", None) == "esp":
            t_translate_start = time.time()
            patient = await llm_batch_translate(patient, "es")
            doctor = await llm_batch_translate(doctor, "es")
            problems = await llm_batch_translate(problems, "es")
            allergies = await llm_batch_translate(allergies, "es")
            vitals = await llm_batch_translate(vitals, "es")
            medications = await llm_batch_translate(medications, "es")
            # For org_id_render and ref_num_render, use LLM for consistency
            org_id_render = (
                (await llm_batch_translate(org_id_render, "es"))
                if org_id_render
                else None
            )
            ref_num_render = (
                (await llm_batch_translate(ref_num_render, "es"))
                if ref_num_render
                else None
            )
            assessment_steps = await llm_batch_translate(assessment_steps, "es")
            t_translate_end = time.time()
            logger.info(
                f"Translation to Spanish took {t_translate_end - t_translate_start:.2f}s (LLM batch)"
            )

        # Centralized dynamic labels for LaTeX template
        vars = {
            "treatment_plan": (
                "PLAN DE TRATAMIENTO" if request.language == "esp" else "TREATMENT PLAN"
            ),
            "reference": "Referencia" if request.language == "esp" else "Reference",
            "patient_name": (
                "Nombre del Paciente" if request.language == "esp" else "Patient Name"
            ),
            "patient_id": (
                "ID del Paciente" if request.language == "esp" else "Patient ID"
            ),
            "dob": "Fecha de Nacimiento" if request.language == "esp" else "DOB",
            "gender": "Género" if request.language == "esp" else "Gender",
            "doctor_name": (
                "Nombre del Doctor" if request.language == "esp" else "Doctor Name"
            ),
            "doctor_id": "ID del Doctor" if request.language == "esp" else "Doctor ID",
            "signature": (
                "Firma del Doctor" if request.language == "esp" else "Doctor Signature"
            ),
            "organization_id": (
                "ID de la Organización"
                if request.language == "esp"
                else "Organization ID"
            ),
            "problem_list": (
                "Lista de Problemas" if request.language == "esp" else "Problem List"
            ),
            "allergies": "Alergias" if request.language == "esp" else "Allergies",
            "allergy_name": (
                "Nombre de la Alergia" if request.language == "esp" else "Allergy Name"
            ),
            "allergy_type": (
                "Tipo de Alergia" if request.language == "esp" else "Allergy Type"
            ),
            "severity_level": (
                "Nivel de Severidad" if request.language == "esp" else "Severity Level"
            ),
            "vitals": "Signos Vitales" if request.language == "esp" else "Vitals",
            "height": "Altura" if request.language == "esp" else "Height",
            "weight": "Peso" if request.language == "esp" else "Weight",
            "bmi": "IMC" if request.language == "esp" else "BMI",
            "heart_rate": (
                "Frecuencia Cardíaca" if request.language == "esp" else "Heart Rate"
            ),
            "blood_pressure": (
                "Presión Arterial" if request.language == "esp" else "Blood Pressure"
            ),
            "active_medications": (
                "Medicamentos Activos"
                if request.language == "esp"
                else "Active Medications"
            ),
            "medication_name": (
                "Nombre del Medicamento"
                if request.language == "esp"
                else "Medication Name"
            ),
            "qty": "Cantidad" if request.language == "esp" else "Qty",
            "dosage": "Dosis" if request.language == "esp" else "Dosage",
            "reason": "Razón" if request.language == "esp" else "Reason",
            "instruction": (
                "Instrucción" if request.language == "esp" else "Instruction"
            ),
            "assessment_plan": (
                "Plan de Evaluación" if request.language == "esp" else "Assessment Plan"
            ),
        }
        # Render LaTeX with all data
        filled_latex = template.render(
            patient=patient,
            doctor=doctor,
            problems=problems,
            allergies=allergies,
            vitals=vitals,
            medications=medications,
            assessment_steps=assessment_steps,
            organization_id=org_id_render,
            reference_number=ref_num_render,
            vars=vars,
        )

        # # Translate to Spanish if requested
        # if getattr(request, 'language', None) == 'esp':
        #     try:
        #         filled_latex = GoogleTranslator(source='auto', target='es').translate(filled_latex)
        #     except Exception as e:
        #         logger.error(f"Translation to Spanish failed: {e}")
        #         raise HTTPException(status_code=500, detail=f"Translation to Spanish failed: {e}")

        # Step 6: Convert LaTeX to PDF and return
        t6 = time.time()
        try:
            pdf_bytes = latex_to_pdf(filled_latex)
            t7 = time.time()
            logger.info(f"PDF generation took {t7-t6:.2f}s")
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": "attachment; filename=treatment_plan.pdf"
                },
            )
        except HTTPException:
            logger.warning("PDF generation failed, falling back to LaTeX file")
            with tempfile.NamedTemporaryFile(
                delete=False, mode="w", suffix=".tex"
            ) as tmp_file:
                tmp_file.write(filled_latex)
                tex_path = tmp_file.name
            return FileResponse(
                tex_path, filename="treatment_plan.tex", media_type="application/x-tex"
            )

    except Exception as e:
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# Global MySQL connection (initialized at startup)
mysql_connection = None


def init_mysql_connection():
    global mysql_connection
    if mysql_connection is None or not mysql_connection.is_connected():
        mysql_connection = mysql.connector.connect(
            host=os.environ.get("MYSQL_HOST"),
            user=os.environ.get("MYSQL_USERNAME"),
            password=os.environ.get("MYSQL_PASSWORD"),
            database=os.environ.get("MYSQL_DATABASE"),
        )
        logger.info("MySQL connection established at startup.")


def replace_underscores(obj):
    if isinstance(obj, dict):
        return {k: replace_underscores(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [replace_underscores(i) for i in obj]
    elif isinstance(obj, str):
        return obj.replace("_", " ")
    else:
        return obj


def latex_to_pdf(latex_content: str) -> bytes:
    """
    Convert LaTeX content to PDF using pdflatex
    Returns PDF bytes or raises HTTPException on failure
    """
    try:
        # Create temporary directory for LaTeX compilation
        with tempfile.TemporaryDirectory() as temp_dir:
            # Write LaTeX content to temporary file
            tex_file_path = os.path.join(temp_dir, "document.tex")
            with open(tex_file_path, "w", encoding="utf-8") as f:
                f.write(latex_content)

            # Run pdflatex to compile the document
            result = subprocess.run(
                [
                    "pdflatex",
                    "-interaction=nonstopmode",
                    "-output-directory",
                    temp_dir,
                    tex_file_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,  # 30 second timeout
            )

            # Check if compilation was successful
            pdf_file_path = os.path.join(temp_dir, "document.pdf")
            if not os.path.exists(pdf_file_path):
                logger.error(f"LaTeX compilation failed: {result.stderr}")
                raise HTTPException(status_code=500, detail="LaTeX compilation failed")

            # Read the generated PDF
            with open(pdf_file_path, "rb") as f:
                pdf_bytes = f.read()

            return pdf_bytes

    except subprocess.TimeoutExpired:
        logger.error("LaTeX compilation timed out")
        raise HTTPException(status_code=500, detail="LaTeX compilation timed out")
    except FileNotFoundError:
        logger.error(
            "pdflatex not found. Please install LaTeX distribution (e.g., TeX Live)"
        )
        raise HTTPException(status_code=500, detail="LaTeX distribution not installed")
    except Exception as e:
        logger.error(f"Error in LaTeX to PDF conversion: {str(e)}")
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")


# Initialize MySQL connection at app startup
init_mysql_connection()

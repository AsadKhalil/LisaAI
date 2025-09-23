import json
import re
import logging
import sys
import traceback
from typing import Any, List
import app.src.constants as constants
from typing_extensions import Annotated
from fastapi import Depends, Response, UploadFile, HTTPException, APIRouter, File, Form, Request
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
    SchedulingCancelRequest,
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
import uuid

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
async def get_chatbot_response(query: Query, request: Request):
    """route definition for chatbot"""
    try:
        start_time = time.time()
        logger.info(f"User's query: {query.input}")
        logger.info(f"Language: {query.language}")
        
        # Get user ID from request header
        user_id = request.headers.get("userId")
        if user_id:
            try:
                user_id = int(user_id)
                logger.info(f"Using user ID from header: {user_id}")
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid user ID in header")
        else:
            # Fallback to request body user ID if not in header
            user_id = int(query.userId) if getattr(query, "userId", None) not in (None, "") else None
            logger.warning(f"Using user ID from request body: {user_id} (consider using header)")
        
        if user_id is None:
            raise HTTPException(status_code=401, detail="No valid user ID found")
        
        llm = await LLMAgentFactory().create()
        if type(llm) == str:
            return llm
        
        await llm._build_prompt(query.language)
        
        # Set the user_id for the agent if it's an OPENAIAgent
        if hasattr(llm, 'set_user_id') and user_id is not None:
            llm.set_user_id(user_id)
            logger.info(f"Set user_id {user_id} for LLM agent")
        
        await llm._create_agent()

        # Scheduling intent detection and auto-start/continue flow
        try:
            lower_q = query.input
            
            # Get chat history for context
            chat_history_context = ""
            if query.chat_history and len(query.chat_history) > 0:
                chat_history_context = "\n\nPrevious conversation context:\n"
                for i, msg in enumerate(query.chat_history[-5:]):  # Last 5 messages for context
                    chat_history_context += f"User: {msg.get('prompt', '')}\n"
                    chat_history_context += f"Assistant: {msg.get('response', '')}\n"
            
            # Enhanced intent classification prompt with detailed procedure
            logger.info(f"chat_history_context: {chat_history_context}")
            intent_prompt = (
                "You are an intent classifier. Determine if the user's message is about scheduling an appointment. Check the history aswell."
                "Return only one word, exactly 'true' or 'false' (lowercase, no punctuation, no explanation).\n\n"
                "if the history has questions like whats the appointment title, appointment type etc, anything that shows the conversation has scheduling content but did not complete and the user's now input is related to the last question, return true"
                "if the user is asking for summarisation of encounters, or asking anything, return false, because then the user is querying the chatbot"
                f"User message: {lower_q}\n\n"
                f"Previous conversation context: {chat_history_context}"
            )
            llm_intent_raw = await simple_openai_chat(intent_prompt)
            llm_intent_text = (llm_intent_raw or "").strip().lower()
            logger.info(f"llm_intent_text: {llm_intent_text}")
            # Robust parse: accept 'true' if it appears as a standalone token; otherwise false
            is_scheduling = bool(re.search(r"\btrue\b", llm_intent_text)) and not bool(re.search(r"\bfalse\b", llm_intent_text))
            logger.info(f"is_scheduling: {is_scheduling}")
            
            if is_scheduling:
                logger.info("[generate->scheduling] Scheduling intent detected; handling via scheduling service")
                from app.src.modules.scheduling import SchedulingService
                service = scheduling_service if 'scheduling_service' in globals() else SchedulingService()
                
                def _format_summary_text(raw_summary):
                    try:
                        data = raw_summary
                        if isinstance(raw_summary, str):
                            try:
                                data = json.loads(raw_summary)
                            except Exception:
                                data = raw_summary
                        # Dict formatting with preferred key order
                        if isinstance(data, dict):
                            # Expand nested answers if present
                            if isinstance(data.get("answers"), dict):
                                answers = data.get("answers", {})
                                key_label_map = {
                                    "appointment_title": "Title",
                                    "appointment_type": "Type",
                                    "consultant_type": "Consultant Type",
                                    "appointment_location": "Location",
                                    "practitioner_selection": "Provider",
                                    "time_selection": "Time",
                                }
                                ordered_keys = [
                                    "appointment_title",
                                    "appointment_type",
                                    "consultant_type",
                                    "appointment_location",
                                    "practitioner_selection",
                                    "time_selection",
                                ]
                                lines = []
                                for k in ordered_keys:
                                    if k in answers and answers[k] not in (None, ""):
                                        label = key_label_map.get(k, k)
                                        lines.append(f"- {label}: {answers[k]}")
                                # Append extra known top-level fields if present
                                for top_key, label in [
                                    ("appointmentTitle", "Title"),
                                    ("appointmentType", "Type"),
                                    ("appointmentDate", "Date"),
                                    ("appointmentTime", "Time"),
                                    ("appointmentDuration", "Duration"),
                                    ("appointmentLocation", "Location"),
                                    ("providerName", "Provider"),
                                    ("reason", "Reason"),
                                ]:
                                    if data.get(top_key) not in (None, ""):
                                        lines.append(f"- {label}: {data[top_key]}")
                                # If summary_text provided, add it at end
                                if data.get("summary_text"):
                                    lines.append(f"- Notes: {data['summary_text']}")
                                return ("Summary:\n" + "\n".join(lines)).strip()
                            preferred_order = [
                                ("appointmentTitle", "Title"),
                                ("appointmentType", "Type"),
                                ("appointmentDate", "Date"),
                                ("appointmentTime", "Time"),
                                ("appointmentDuration", "Duration"),
                                ("appointmentLocation", "Location"),
                                ("providerName", "Provider"),
                                ("reason", "Reason"),
                            ]
                            lines = []
                            added = set()
                            for key, label in preferred_order:
                                if key in data and data[key] not in (None, ""):
                                    lines.append(f"- {label}: {data[key]}")
                                    added.add(key)
                            # Add any remaining fields
                            for key, value in data.items():
                                if key in ("session_id", "answers"):
                                    continue
                                if key not in added and value not in (None, ""):
                                    lines.append(f"- {key}: {value}")
                            return ("Summary:\n" + "\n".join(lines)).strip()
                        # List formatting
                        if isinstance(data, list):
                            lines = []
                            for item in data:
                                if isinstance(item, dict):
                                    item_str = ", ".join([f"{k}: {v}" for k, v in item.items() if v not in (None, "")])
                                    lines.append(f"- {item_str}")
                                else:
                                    lines.append(f"- {item}")
                            return ("Summary:\n" + "\n".join(lines)).strip()
                        # String fallback - tidy up
                        text = str(raw_summary or "").strip()
                        if not text:
                            return "Summary:\n- No details available"
                        cleaned_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                        beautified = []
                        for ln in cleaned_lines:
                            if ":" in ln and not ln.lstrip().startswith("-"):
                                beautified.append(f"- {ln}")
                            else:
                                beautified.append(ln)
                        return ("Summary:\n" + "\n".join(beautified)).strip()
                    except Exception:
                        return f"Summary:\n{raw_summary}"
                
                # Get current session (if any)
                current = await service.get_current_session(user_id)
                
                if not current:
                    # No active session - start new one
                    logger.info("[generate->scheduling] No active session; starting new session")
                    current = await service.start_scheduling_session(user_id)
                    response_payload = {
                        "status": "next_question",
                        "session_id": current.session_id,
                        "current_question": current.next_question.question,
                        "step_type": current.next_question.step_type,
                        "options": current.next_question.options,
                        "message": "Scheduling session started. Please answer the following question:"
                    }
                    logger.info(f"[generate->scheduling] Responding with first question sessionId={current.session_id}")
                    response_text_parts = [
                        response_payload.get("message", ""),
                        f"Question: {response_payload.get('current_question', '')}",
                    ]
                    if response_payload.get("options"):
                        options_list = response_payload.get("options") or []
                        options_block = "\n".join([f"- {opt}" for opt in options_list])
                        response_text_parts.append("Options:\n" + options_block)
                    response_text = "\n\n".join([p for p in response_text_parts if p])
                    wrapped_response = {
                        "response": response_text,
                        "query_id": "",
                        "convo_id": query.convo_id or "",
                    }
                    return json.dumps(wrapped_response)
                
                # Active session exists - handle based on status
                if current.status == "ready_for_confirmation":
                    # Handle confirmation intent
                    logger.info(f"[generate->scheduling] Session ready for confirmation sessionId={current.session_id}")
                    confirm_keywords = ["confirm", "yes", "book it", "looks good", "proceed", "schedule"]
                    cancel_keywords = ["cancel", "stop", "discard", "abort", "no"]
                    
                    if any(k in lower_q for k in confirm_keywords):
                        appointment_result = await service.confirm_appointment(user_id, current.session_id)
                        appointment_payload = appointment_result["payload"]
                        appointment_summary = appointment_result["summary"]
                        db_result = appointment_result["db_result"]
                        
                        if db_result["success"]:
                            response_payload = {
                                "status": "confirmed",
                                "appointment": {
                                    "id": str(db_result.get("appointment_id", uuid.uuid4())),
                                    "date": appointment_payload["appointmentDate"],
                                    "time": appointment_payload["appointmentTime"],
                                    "reason": appointment_payload.get("reason", ""),
                                    "duration": appointment_payload.get("appointmentDuration", ""),
                                    "notes": appointment_payload.get("reason", "")
                                },
                                "message": "Appointment scheduled successfully!",
                                "summary": appointment_summary
                            }
                        else:
                            response_payload = {
                                "status": "failed",
                                "message": f"Failed to schedule appointment: {db_result.get('error', 'Unknown error')}",
                                "details": db_result.get("message", ""),
                                "summary": appointment_summary
                            }
                        logger.info(f"[generate->scheduling] Appointment confirmed sessionId={current.session_id}")
                        if response_payload.get("status") == "confirmed":
                            response_text = "Booking done, thank you"
                        else:
                            response_text = f"{response_payload.get('message', '')}\n{response_payload.get('details', '')}".strip()
                        wrapped_response = {
                            "response": response_text,
                            "query_id": "",
                            "convo_id": query.convo_id or "",
                        }
                        return json.dumps(wrapped_response)
                    
                    if any(k in lower_q for k in cancel_keywords):
                        await service.cancel_session(user_id, current.session_id)
                        response_payload = {
                            "status": "cancelled",
                            "message": "Appointment scheduling cancelled."
                        }
                        logger.info(f"[generate->scheduling] Appointment cancelled sessionId={current.session_id}")
                        wrapped_response = {
                            "response": response_payload.get("message", "Appointment scheduling cancelled."),
                            "query_id": "",
                            "convo_id": query.convo_id or "",
                        }
                        return json.dumps(wrapped_response)
                    
                    # Show summary again
                    summary = await service.get_session_summary(user_id, current.session_id)
                    response_payload = {
                        "status": "ready_for_confirmation",
                        "session_id": current.session_id,
                        "summary": summary,
                        "message": "All questions answered! Please review and confirm your appointment details. Reply 'confirm' to proceed or 'cancel' to abort."
                    }
                    summary_text = _format_summary_text(response_payload.get("summary", ""))
                    response_text = (
                        f"{response_payload.get('message', '')}\n\n{summary_text}"
                    ).strip()
                    wrapped_response = {
                        "response": response_text,
                        "query_id": "",
                        "convo_id": query.convo_id or "",
                    }
                    return json.dumps(wrapped_response)
                
                # Active session with pending questions - submit answer
                if current.next_question is None:
                    # Defensive: session says active but no next_question; send summary prompt
                    summary = await service.get_session_summary(user_id, current.session_id)
                    response_payload = {
                        "status": "ready_for_confirmation",
                        "session_id": current.session_id,
                        "summary": summary,
                        "message": "All questions answered! Please review and confirm your appointment details. Reply 'confirm' to proceed or 'cancel' to abort."
                    }
                    logger.warning(f"[generate->scheduling] Missing next_question; treating as ready_for_confirmation sessionId={current.session_id}")
                    summary_text = _format_summary_text(response_payload.get("summary", ""))
                    response_text = (
                        f"{response_payload.get('message', '')}\n\n{summary_text}"
                    ).strip()
                    wrapped_response = {
                        "response": response_text,
                        "query_id": "",
                        "convo_id": query.convo_id or "",
                    }
                    return json.dumps(wrapped_response)

                # Submit answer to current question
                step_id = current.next_question.step_id
                answer_text = query.input
                
                # Validate multiple choice answers
                if getattr(current, "options", None):
                    options = current.options or []
                    if answer_text not in options:
                        response_payload = {
                            "status": "validation_error",
                            "session_id": current.session_id,
                            "current_question": current.next_question.question,
                            "step_type": current.next_question.step_type,
                            "options": options,
                            "message": "Please choose one of the options exactly as shown."
                        }
                        logger.info(f"[generate->scheduling] Invalid option provided for step {step_id}")
                        response_text_parts = [
                            response_payload.get("message", ""),
                            f"Question: {response_payload.get('current_question', '')}",
                        ]
                        if response_payload.get("options"):
                            options_list = response_payload.get("options") or []
                            options_block = "\n".join([f"- {opt}" for opt in options_list])
                            response_text_parts.append("Options:\n" + options_block)
                        response_text = "\n\n".join([p for p in response_text_parts if p])
                        wrapped_response = {
                            "response": response_text,
                            "query_id": "",
                            "convo_id": query.convo_id or "",
                        }
                        return json.dumps(wrapped_response)

                # Submit answer
                logger.info(f"[generate->scheduling] Submitting answer for sessionId={current.session_id} stepId={step_id}")
                updated = await service.submit_answer(user_id, current.session_id, step_id, answer_text)
                
                if updated.status == "ready_for_confirmation":
                    summary = await service.get_session_summary(user_id, current.session_id)
                    response_payload = {
                        "status": "ready_for_confirmation",
                        "session_id": current.session_id,
                        "summary": summary,
                        "message": "All questions answered! Please review and confirm your appointment details. Reply 'confirm' to proceed or 'cancel' to abort."
                    }
                    logger.info(f"[generate->scheduling] Reached confirmation sessionId={current.session_id}")
                    summary_text = _format_summary_text(response_payload.get("summary", ""))
                    response_text = (
                        f"{response_payload.get('message', '')}\n\n{summary_text}"
                    ).strip()
                    wrapped_response = {
                        "response": response_text,
                        "query_id": "",
                        "convo_id": query.convo_id or "",
                    }
                    return json.dumps(wrapped_response)
                else:
                    response_payload = {
                        "status": "next_question",
                        "session_id": current.session_id,
                        "current_question": updated.next_question.question if updated.next_question else None,
                        "step_type": updated.next_question.step_type if updated.next_question else None,
                        "options": updated.next_question.options if updated.next_question else None,
                        "message": "Thank you! Here's the next question:"
                    }
                    logger.info(f"[generate->scheduling] Next question sessionId={current.session_id}")
                    response_text_parts = [
                        response_payload.get("message", ""),
                        f"Question: {response_payload.get('current_question', '')}",
                    ]
                    if response_payload.get("options"):
                        options_list = response_payload.get("options") or []
                        options_block = "\n".join([f"- {opt}" for opt in options_list])
                        response_text_parts.append("Options:\n" + options_block)
                    response_text = "\n\n".join([p for p in response_text_parts if p])
                    wrapped_response = {
                        "response": response_text,
                        "query_id": "",
                        "convo_id": query.convo_id or "",
                    }
                    return json.dumps(wrapped_response)
                    
        except Exception as schedule_e:
            logger.error(f"[generate->scheduling] Error handling scheduling intent: {str(schedule_e)}")
            # Fall through to normal chat on error

        # if current_user.custom_claims.get('local_id') is not None:
        #     user_id = current_user.custom_claims.get('local_id')
        #     logger.info(f"Current user's local id: {user_id}")
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

        # chatbot's response - use original query without embedding user_id in prompt
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
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/appointments/{user_id}")
async def get_user_appointments(user_id: int):
    """Get all appointments for a specific user"""
    try:
        from app.src.modules.databases import ConversationDB
        db = ConversationDB()
        appointments = await db.get_user_appointments(str(user_id))
        
        formatted_appointments = []
        for appointment in appointments:
            formatted_appointments.append({
                "id": appointment[0],
                "appointmentTitle": appointment[1],
                "appointmentType": appointment[2],
                "patientId": appointment[3],
                "appointmentLocation": appointment[4],
                "providerId": appointment[5],
                "providerName": appointment[6],
                "statusCode": appointment[7],
                "appointmentDate": appointment[8].isoformat() if appointment[8] else None,
                "appointmentTime": appointment[9],
                "appointmentDuration": appointment[10],
                "reason": appointment[11],
                "isRecurring": appointment[12],
                "typeCode": appointment[13],
                "isDeleted": appointment[14],
                "isActive": appointment[15],
                "createdById": appointment[16],
                "updatedById": appointment[17],
                "deletedById": appointment[18],
                "createdAt": appointment[19].isoformat() if appointment[19] else None,
                "updatedAt": appointment[20].isoformat() if appointment[20] else None,
                "startDateTime": appointment[21].isoformat() if appointment[21] else None,
                "endDateTime": appointment[22].isoformat() if appointment[22] else None,
                "recurringSettingId": appointment[23],
                "appointmentDate_utc": appointment[24].isoformat() if appointment[24] else None,
                "startDateTime_utc": appointment[25].isoformat() if appointment[25] else None,
                "endDateTime_utc": appointment[26].isoformat() if appointment[26] else None,
                "appointmentMode": appointment[27],
                "consultantType": appointment[28]
            })
        
        return {
            "user_id": user_id,
            "appointments": formatted_appointments,
            "total": len(formatted_appointments)
        }
        
    except Exception as e:
        logger.error(f"Error getting appointments for user {user_id}: {str(e)}")
        logger.error(traceback.format_exc())
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
        llm_response = await simple_openai_chat(assessment_prompt)
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
            response = await simple_openai_chat(prompt)
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

def extract_encounter_data(user_id):
    """
    Extract comprehensive encounter data for a user from database tables.
    This function is called by the LLM when it understands the user wants encounter summaries.
    The user_id IS the patient ID - there's no separate patient concept.
    
    Args:
        user_id (int): The user ID is responsible for getting the 'id' of patient, which is then used as FK in other tables (which is also the patient ID)
        
    Returns:
        dict: Comprehensive user encounter data or None if error
    """
    try:
        global mysql_connection
        if mysql_connection is None or not mysql_connection.is_connected():
            init_mysql_connection()
        
        connection = mysql_connection
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT id
            FROM patients
            WHERE userId = %s
        """, (user_id,))

        result = cursor.fetchone()  # e.g. {'id': 95}
        if result is None:
            return f"No patient record found for user ID {user_id}. Please ensure you have a valid patient account."
        
        patient_id = result["id"]
        # Extract patient encounters (filtered by user_id for security)
        cursor.execute("""
            SELECT id, patientId, encounterTypeCode, encounterTypeHistory, 
                   startDate, endDate, assignedToId, billingTypeCode, 
                   billingId, duration, additionalFields, soapForm, 
                   selectedForms, signature, atDraft, isDeleted, isActive, 
                   createdById, updatedById, deletedById, createdAt, 
                   updatedAt, isLock, patientName
            FROM patient_encounters 
            WHERE patientId = %s AND isDeleted = 0 AND isActive = 1
            ORDER BY createdAt DESC
        """, (patient_id,))
        encounters = cursor.fetchall()
        
        # Extract allergies (filtered by user_id for security)
        cursor.execute("""
            SELECT allergy, severitiesCode, dateOfOnSet, patientEncounterId, 
                   comment, isDeleted, isActive, allergiesStatus, allergytype, createdById
            FROM allergies 
            WHERE patientId = %s AND isDeleted = 0
        """, (patient_id,))
        allergies = cursor.fetchall()
        
        # Extract recent vitals (last 5 records, filtered by user_id for security)
        cursor.execute("""
            SELECT recordDate, recordTime, weightKilo, weightGram, weightUnit,
                   heightFt, heightIn, heightUnit, bmi, temperatureF, 
                   systolicBloodPressure, diastolicBloodPressure, respiratoryRate,
                   pulseBpm, bloodSugar, fasting, o2Saturation, createdById
            FROM vitals 
            WHERE patientId = %s AND isDeleted = 0 AND isActive = 1
            ORDER BY createdAt DESC
            LIMIT 5
        """, (patient_id,))
        vitals = cursor.fetchall()
        
        # Extract current medications (filtered by user_id for security)
        cursor.execute("""
            SELECT medicationtype, drugname, drugbrandname, instruction, 
                   quantity, refill, isallowsubstitution, prescriber, 
                   prescriberid, doctor_id, startedon, isadministered, 
                   reason, discontinuecomment, errorcomment, cancelrxcomment,
                   dose, unit, route, frequency, duration, direction, comment, createdby
            FROM patient_medications 
            WHERE patient_id = %s AND isdeleted = 0 AND isactive = 1
        """, (patient_id,))
        medications = cursor.fetchall()
        
        cursor.close()
        
        # Structure the comprehensive encounter data
        encounter_data = {
            "user_id": user_id,
            "patient_id": patient_id,
            "total_encounters": len(encounters),
            "encounters": encounters,
            "allergies": allergies,
            "recent_vitals": vitals,
            "current_medications": medications,
            "data_extracted_at": datetime.now().isoformat()
        }
        
        logger.info(f"Successfully extracted encounter data for patient with patient_id {patient_id}")
        return encounter_data
        
    except Exception as e:
        logger.error(f"Error extracting encounter data for user_id {user_id}: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error retrieving encounter data: {str(e)}"
        
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

# Import scheduling service
from app.src.modules.scheduling import SchedulingService

# Initialize scheduling service
scheduling_service = SchedulingService()

# Scheduling endpoints
@router.post("/schedule/start")
async def start_scheduling_session(request: Request):
    """Start a new scheduling session"""
    try:
        # Get user ID and name from request headers
        logger.info("[schedule/start] Request received")
        user_id = request.headers.get("userId")
        user_name = request.headers.get("userName", "User")
        logger.info(f"[schedule/start] Headers userId={user_id}")
        
        if not user_id:
            raise HTTPException(status_code=400, detail="userId header is required")
        
        try:
            user_id = int(user_id)
            logger.info(f"[schedule/start] Parsed userId={user_id}")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID format")
        
        # Start scheduling session
        logger.info(f"[schedule/start] Starting session for userId={user_id}")
        session = await scheduling_service.start_scheduling_session(user_id)
        logger.info(
            f"[schedule/start] Session started sessionId={session.session_id} nextStep={getattr(session.next_question, 'step_type', None)}"
        )
        
        response_payload = {
            "session_id": session.session_id,
            "current_question": session.next_question.question,
            "step_type": session.next_question.step_type,
            "options": session.next_question.options,
            "message": "Scheduling session started. Please answer the following question:"
        }
        logger.info(
            f"[schedule/start] Responding sessionId={response_payload['session_id']} stepType={response_payload['step_type']}"
        )
        return response_payload
        
    except Exception as e:
        logger.error(f"Error starting scheduling session: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/schedule/answer")
async def submit_scheduling_answer(request: Request):
    """Submit an answer to the current scheduling question"""
    try:
        # Get user ID from request headers
        logger.info("[schedule/answer] Request received")
        user_id = request.headers.get("userId")
        logger.info(f"[schedule/answer] Headers userId={user_id}")
        if not user_id:
            raise HTTPException(status_code=400, detail="userId header is required")
        
        try:
            user_id = int(user_id)
            logger.info(f"[schedule/answer] Parsed userId={user_id}")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID format")
        
        # Get request body
        body = await request.json()
        session_id = body.get("session_id")
        step_id = body.get("step_id")
        answer = body.get("answer")
        logger.info(
            f"[schedule/answer] Body sessionId={session_id} stepId={step_id} answerType={type(answer).__name__}"
        )
        
        if not all([session_id, step_id, answer]):
            raise HTTPException(status_code=400, detail="session_id, step_id, and answer are required")
        
        # Submit answer
        logger.info(f"[schedule/answer] Submitting answer for sessionId={session_id} stepId={step_id}")
        session = await scheduling_service.submit_answer(user_id, session_id, step_id, answer)
        logger.info(
            f"[schedule/answer] Updated session status={session.status} nextStep={(session.next_question.step_type if session.next_question else None)}"
        )
        
        if session.status == "ready_for_confirmation":
            # Get summary for confirmation
            logger.info(f"[schedule/answer] Session ready for confirmation sessionId={session_id}")
            summary = await scheduling_service.get_session_summary(user_id, session_id)
            response_payload = {
                "status": "ready_for_confirmation",
                "session_id": session_id,
                "summary": summary,
                "message": "All questions answered! Please review and confirm your appointment details."
            }
            logger.info(f"[schedule/answer] Responding with confirmation-ready sessionId={session_id}")
            return response_payload
        else:
            # Return next question
            response_payload = {
                "status": "next_question",
                "session_id": session_id,
                "current_question": session.next_question.question,
                "step_type": session.next_question.step_type,
                "options": session.next_question.options,
                "message": "Thank you! Here's the next question:"
            }
            logger.info(
                f"[schedule/answer] Responding next question sessionId={session_id} stepType={response_payload['step_type']}"
            )
            return response_payload
        
    except Exception as e:
        logger.error(f"Error submitting scheduling answer: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/schedule/confirm")
async def confirm_appointment(request: Request):
    """Confirm and create the appointment"""
    try:
        # Get user ID and name from request headers
        logger.info("[schedule/confirm] Request received")
        user_id = request.headers.get("userId")
        user_name = request.headers.get("userName", "User")
        logger.info(f"[schedule/confirm] Headers userId={user_id} userName={user_name}")
        
        if not user_id:
            raise HTTPException(status_code=400, detail="userId header is required")
        
        try:
            user_id = int(user_id)
            logger.info(f"[schedule/confirm] Parsed userId={user_id}")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID format")
        
        # Get request body
        body = await request.json()
        session_id = body.get("session_id")
        confirm = body.get("confirm", False)
        logger.info(f"[schedule/confirm] Body sessionId={session_id} confirm={confirm}")
        
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        
        if not confirm:
            # Cancel the session
            logger.info(f"[schedule/confirm] Cancelling session sessionId={session_id}")
            await scheduling_service.cancel_session(user_id, session_id)
            response_payload = {
                "status": "cancelled",
                "message": "Appointment scheduling cancelled."
            }
            logger.info(f"[schedule/confirm] Responding cancelled sessionId={session_id}")
            return response_payload
        
        # Confirm appointment
        logger.info(f"[schedule/confirm] Confirming appointment sessionId={session_id}")
        appointment_result = await scheduling_service.confirm_appointment(user_id, session_id)
        appointment_payload = appointment_result["payload"]
        appointment_summary = appointment_result["summary"]
        db_result = appointment_result["db_result"]
        
        if db_result["success"]:
            logger.info(
                f"[schedule/confirm] Appointment confirmed date={appointment_payload['appointmentDate']} time={appointment_payload['appointmentTime']} duration={appointment_payload.get('appointmentDuration', '')} id={db_result.get('appointment_id')}"
            )
            
            response_payload = {
                "status": "confirmed",
                "appointment": {
                    "id": str(db_result.get("appointment_id", uuid.uuid4())),
                    "date": appointment_payload["appointmentDate"],
                    "time": appointment_payload["appointmentTime"],
                    "reason": appointment_payload.get("reason", ""),
                    "duration": appointment_payload.get("appointmentDuration", ""),
                    "notes": appointment_payload.get("reason", "")
                },
                "message": "Appointment scheduled successfully!",
                "summary": appointment_summary
            }
        else:
            logger.error(f"[schedule/confirm] Appointment failed: {db_result.get('error', 'Unknown error')}")
            
            response_payload = {
                "status": "failed",
                "message": f"Failed to schedule appointment: {db_result.get('error', 'Unknown error')}",
                "details": db_result.get("message", ""),
                "summary": appointment_summary
            }
        logger.info("[schedule/confirm] Responding confirmed appointment")
        return response_payload
        
    except Exception as e:
        logger.error(f"Error confirming appointment: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/schedule/cancel")
async def cancel_scheduling_session(request: Request):
    """Cancel a scheduling session"""
    try:
        # Get user ID from request headers
        logger.info("[schedule/cancel] Request received")
        user_id = request.headers.get("userId")
        logger.info(f"[schedule/cancel] Headers userId={user_id}")
        if not user_id:
            raise HTTPException(status_code=400, detail="userId header is required")
        
        try:
            user_id = int(user_id)
            logger.info(f"[schedule/cancel] Parsed userId={user_id}")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID format")
        
        # Get request body
        body = await request.json()
        session_id = body.get("session_id")
        logger.info(f"[schedule/cancel] Body sessionId={session_id}")
        
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        
        # Cancel session
        logger.info(f"[schedule/cancel] Cancelling session sessionId={session_id}")
        success = await scheduling_service.cancel_session(user_id, session_id)
        
        if success:
            response_payload = {
                "status": "cancelled",
                "message": "Scheduling session cancelled successfully."
            }
            logger.info(f"[schedule/cancel] Responding cancelled sessionId={session_id}")
            return response_payload
        else:
            raise HTTPException(status_code=500, detail="Failed to cancel session")
        
    except Exception as e:
        logger.error(f"Error cancelling scheduling session: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/schedule/status/{user_id}")
async def get_scheduling_status(user_id: int):
    """Get the current scheduling status for a user"""
    try:
        logger.info(f"[schedule/status] Request received userId={user_id}")
        session = await scheduling_service.get_current_session(user_id)
        
        if not session:
            response_payload = {
                "status": "no_active_session",
                "message": "No active scheduling session found."
            }
            logger.info(f"[schedule/status] No active session userId={user_id}")
            return response_payload
        
        if session.status == "ready_for_confirmation":
            logger.info(f"[schedule/status] Ready for confirmation sessionId={session.session_id}")
            summary = await scheduling_service.get_session_summary(user_id, session.session_id)
            response_payload = {
                "status": "ready_for_confirmation",
                "session_id": session.session_id,
                "summary": summary,
                "message": "Ready to confirm appointment details."
            }
            logger.info(f"[schedule/status] Responding confirmation-ready sessionId={session.session_id}")
            return response_payload
        else:
            response_payload = {
                "status": "in_progress",
                "session_id": session.session_id,
                "current_step": session.current_step,
                "next_question": session.next_question.question if session.next_question else None,
                "step_type": session.next_question.step_type if session.next_question else None,
                "options": session.next_question.options if session.next_question else None,
                "message": "Scheduling session in progress."
            }
            logger.info(
                f"[schedule/status] In progress sessionId={session.session_id} stepType={response_payload['step_type']}"
            )
            return response_payload
        
    except Exception as e:
        logger.error(f"Error getting scheduling status: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
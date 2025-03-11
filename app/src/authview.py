import logging
import sys
import traceback
import psycopg
from typing_extensions import Annotated
from fastapi import Depends, Request, HTTPException, APIRouter
from fastapi.security import OAuth2PasswordBearer
from app.src import constants
from app.src.data_types import GoogleSignup, Login, SignUp
from .modules.databases import ConversationDB
from .modules.auth import Authentication
from dotenv import load_dotenv
from .signup import google_signup
from firebase_admin.auth import UserRecord
import firebase_admin._auth_utils

oauth2scheme = OAuth2PasswordBearer(
    tokenUrl="token",
)

load_dotenv()
auth_router = APIRouter()

logger = logging.getLogger("auth view")


async def get_current_user(token: Annotated[str, Depends(oauth2scheme)]):
    """get current user"""
    try:
        auth = Authentication()
        user = await auth.authenticate_user(token)
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


@auth_router.post("/signup")
async def signup(data: SignUp):
    try:
        db = ConversationDB()
        allowed_emails = await db.allowed_email_addresses()
        allowed_domains = await db.allowed_email_domains()

        logger.info(allowed_emails)
        logger.info(allowed_domains)

        emails = []
        for allowed_email in allowed_emails:
            emails.append(allowed_email[2].strip())

        logger.info(emails)

        domains = []
        for allowed_domain in allowed_domains:
            domains.append(allowed_domain[0])
        logger.info(domains)
        # Extract domain from email
        domain = data.email.split('@')[-1]
        error = False
        role = constants.EMPLOYEE_ROLE

        # Check if domain is not "tkxel.com"
        if domain not in domains:
            error = True
            logger.critical(
                "the email's domain is not in the allowed list of domains")

        if data.email in emails:
            error = False
            i = emails.index(data.email)
            role = allowed_emails[i][3]

            if role is None:
                role = constants.EMPLOYEE_ROLE

            logger.critical(
                "the email's is in the allowed list of emails")

        if error == True:
            raise HTTPException(status_code=400, detail="Invalid email domain")

        response = await db.insert_user(email=data.email, name=data.name, password=data.password, department=data.department, designation=data.designation, role=role)
        return response
    except Exception:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail="Failed to create user")


@auth_router.post("/login")
async def login(data: Login):
    try:
        auth = Authentication()
        user: UserRecord = await auth.get_user_by_email(data.email)
        if user is None:
            raise HTTPException(
                status_code=401, detail="A user with this email does not exist")
        if user.email_verified is False:
            raise HTTPException(status_code=403, detail="Email not verified")
        response = await auth.sign_in_with_email_and_password(email=data.email, password=data.password)
        await auth.update_user({"uid": user.uid, "emailVerified": True})
        response["email_verified"] = user.email_verified
        response["role"] = user.custom_claims.get('role')
        if response["role"] is None:

            db = ConversationDB()
            user_from_db = await db.get_user_by_email(data.email)
            role = user_from_db[0][5]
            response["role"] = role

        return response
    except HTTPException as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except firebase_admin._auth_utils.UserNotFoundError as e:
        logger.exception(traceback.format_exc())
        logger.exception(sys.exc_info()[2])
        raise HTTPException(status_code=401, detail="User not found")
    except Exception:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail="Failed to log-in")


@auth_router.post("/googlesignup")
async def google_signup_endpoint(data: GoogleSignup, request: Request):
    try:
        db = ConversationDB()
        client = request.client
        logger.info(f"Client: {client}")
        return await google_signup(data, db)

    except (ValueError, psycopg.Error):

        logger.exception(traceback.format_exc())
        try:
            auth = Authentication()
            response = await auth.delete_user(data.uid)
            logger.debug(response)
        except HTTPException:
            logger.exception("User was not deleted from firebase")

        raise HTTPException(
            status_code=400, detail="The user is not allowed to signup")

    except HTTPException as e:
        raise HTTPException(
            status_code=e.status_code, detail=e.detail)

    except Exception:
        logger.exception(traceback.format_exc())
        logger.exception(sys.exc_info()[2])
        try:
            auth = Authentication()
            response = await auth.delete_user(data.uid)
            logger.debug(response)
        except HTTPException:
            logger.exception("User was not deleted from firebase")
        raise HTTPException(status_code=500, detail="Failed to create user")

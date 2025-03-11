import ipaddress
from fastapi import HTTPException
from app.src.data_types import GoogleSignup
from app.src.modules.auth import Authentication
from app.src.modules.databases import ConversationDB
import app.src.constants as constants
import logging
import datetime
import json


logger = logging.getLogger(__name__)


async def google_signup(data: GoogleSignup, db: ConversationDB):

    logger.info(data)

    allowed_emails = await db.select_all_from_allowed_email_addresses()
    emails = []
    auth = Authentication()

    for email in allowed_emails:
        emails.append(email[2].strip())

    error = False

    if error == True:
        await auth.revoke_refresh_tokens(data.uid)
        raise HTTPException(
            status_code=401, detail="The user is not allowed to signup from this ip")

    does_user_exist, user = await db.does_user_exist(data.email)

    if does_user_exist is True:
        # This is the senario that the user already exists in the database
        if user[8] is not None:
            # This is the senario that the user already exists in the database and has already signed up using google
            logger.info(
                "User already exists in the database, so only signing in")
            await db.update_user(user[2], "last_login", datetime.datetime.now())
            local_id = user[0]
            local_id_json = json.dumps(str(local_id))
            local_id_json = local_id_json.strip('"')

            await auth.update_custom_claims(
                data.uid, {"role": user[5], "local_id": local_id_json})
            return {"status": "sign-in", "message": "User already exists", "role": user[5]}

        # This is the senario that the user already exists in the database and has already signed up using email and password and now wants to signup using google
        logger.info(
            "User was previously signed up using email and password, and now wants to signup using google, so update his custom claims")

        local_id = user[0]
        local_id_json = json.dumps(str(local_id))
        local_id_json = local_id_json.strip('"')

        await auth.update_custom_claims(
            user[0], {"role": user[3], "local_id": local_id_json})
        await db.update_user(local_id, "firebase_uid", data.uid)
        logger.info(
            "User already exists, updated custom claims, and inserted firebase id to database")
        return {"status": "sign-in", "message": "User already exists, updated custom claims, and inserted firebase id to database", "role": user[5]}

    error = True
    role = constants.EMPLOYEE_ROLE

    if data.email in emails:
        error = False
        i = emails.index(data.email)
        role = allowed_emails[i][3]

        if role is None:
            role = constants.EMPLOYEE_ROLE

        logger.critical(
            "the email's is in the allowed list of emails")

    if error == True:
        await auth.revoke_refresh_tokens(data.uid)
        raise ValueError("The user is not allowed to signup")

    local_id = await db.insert_google_user(data.name, data.email, data.uid, role)
    auth = Authentication()

    user = await auth.get_user_by_uid(data.uid)
    custom_claims = user.custom_claims
    logger.info(custom_claims)
    if custom_claims is None:
        custom_claims = {}
    custom_claims["role"] = role
    local_id_json = json.dumps(str(local_id))
    local_id_json = local_id_json.strip('"')

    custom_claims["local_id"] = local_id_json
    logger.info(custom_claims)
    await auth.update_custom_claims(data.uid, custom_claims)

    return {"status": "success", "message": "User signed up successfully", "role": role}

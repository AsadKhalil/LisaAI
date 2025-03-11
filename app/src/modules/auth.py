import asyncio
import json
import logging
import os
import sys
import traceback
import requests
import firebase_admin
from firebase_admin import auth
from firebase_admin.auth import UserRecord
from requests.exceptions import HTTPError
import app.src.constants as constants


def raise_detailed_error(request_object):
    try:
        request_object.raise_for_status()
    except HTTPError as e:
        # raise detailed error message
        # TODO: Check if we get a { "error" : "Permission denied." } and handle automatically
        raise HTTPError(e, request_object.text)


class Authentication:
    def __init__(self):
        if not firebase_admin._apps:
            path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            print(path)
            cred = firebase_admin.credentials.Certificate(path)
            self.default_app = firebase_admin.initialize_app(credential=cred)
        self.logger = logging.getLogger("Authentication")
        self.api_key = os.getenv(constants.FIREBASE_API_KEY)

    async def signup(self, id, email, name, password, role):
        # try:
        user = auth.create_user(
            email=email,
            password=password,
            display_name=name,
            disabled=False,
            email_verified=False,
            uid=id
        )

        self.logger.info(user)
        self.logger.info(role)
        custom_claim_response = auth.set_custom_user_claims(id, {"role": role})
        self.logger.info(custom_claim_response)
        return user
        # except Exception:
        #     self.logger.exception(traceback.format_exc())
        #     self.logger.exception(sys.exc_info()[2])

    async def update_user(self, data):
        try:
            updatedata = {}

            if data.get("uid") is not None:
                updatedata["uid"] = data["uid"]
            if data.get("name") is not None:
                updatedata["display_name"] = data["name"]
            if data.get("emailVerified") is not None:
                updatedata["email_verified"] = data["emailVerified"]

            user = auth.update_user(**updatedata)
            return user
        except Exception:
            self.logger.exception(traceback.format_exc())
            self.logger.exception(sys.exc_info()[2])

    async def authenticate_user(self, token):
        # try:
        result = auth.verify_id_token(token, check_revoked=True)
        user = auth.get_user(result["uid"])
        return user
        # except Exception:
        #     self.logger.exception(traceback.format_exc())
        #     self.logger.exception(sys.exc_info()[2])

    async def get_user_by_email(self, email):
        user = auth.get_user_by_email(email)
        return user

    async def sign_in_with_email_and_password(self, email, password):
        request_ref = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={0}".format(
            self.api_key)
        headers = {"content-type": "application/json; charset=UTF-8"}
        data = json.dumps(
            {"email": email, "password": password, "returnSecureToken": True})
        request_object = requests.post(request_ref, headers=headers, data=data)
        raise_detailed_error(request_object)
        self.current_user = request_object.json()
        return request_object.json()

    async def sign_out_user(self, id):
        request_ref = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/signOutUser?key={0}".format(
            self.api_key)
        headers = {"content-type": "application/json; charset=UTF-8"}
        data = json.dumps(
            {"localId": id})
        request_object = requests.post(request_ref, headers=headers, data=data)
        raise_detailed_error(request_object)
        self.current_user = request_object.json()
        return request_object.json()

    async def delete_user(self, id):
        try:
            return auth.delete_user(id)
        except Exception as e:
            self.logger.exception(traceback.format_exc())
            self.logger.exception(sys.exc_info()[2])
            raise e

    async def update_custom_claims(self, id, custom_claims):
        return auth.set_custom_user_claims(id, custom_claims)

    async def attach_role_to_user(self, uid, role):
        user = auth.get_user(uid)
        custom_claims = user.custom_claims
        if custom_claims is None:
            custom_claims = {}
            self.logger.critical("the user's Custom claims are None")
        custom_claims["role"] = role
        return auth.set_custom_user_claims(uid, custom_claims=custom_claims)

    async def get_user_by_uid(self, uid):
        return auth.get_user(uid)

    async def attach_default_role_to_users(self):
        try:
            users_list = auth.list_users()
            for user in users_list.users:
                print(f"User ID: {user.uid}")
                print(f"Email: {user.email}")
                auth.set_custom_user_claims(user.uid, {"role": "Default"})
                print("\n")
            print("Default role attached to all users")
        except Exception:
            self.logger.exception(traceback.format_exc())
            self.logger.exception(sys.exc_info()[2])

    async def make_admin(self, email):
        try:
            users_list = auth.list_users()
            for user in users_list.users:
                if user.email == email:
                    auth.set_custom_user_claims(user.uid, {"role": "Admin"})
                    print(f"User ID: {user.uid}",
                          f"Email: {user.email}", " elevated to Admin")
                    return True
            return False
        except Exception:
            self.logger.exception(traceback.format_exc())
            self.logger.exception(sys.exc_info()[2])

    async def make_default(self, email):
        users_list = auth.list_users()
        for user in users_list.users:
            if user.email == email:
                auth.set_custom_user_claims(user.uid, {"role": "Default"})
                print(f"User ID: {user.uid}",
                      f"Email: {user.email}", " demoted to Default")
                return True
        return False

    async def send_email_verification(self, id_token):
        request_ref = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/getOobConfirmationCode?key={0}".format(
            self.api_key)
        headers = {"content-type": "application/json; charset=UTF-8"}
        data = json.dumps({"requestType": "VERIFY_EMAIL", "idToken": id_token})
        request_object = requests.post(request_ref, headers=headers, data=data)
        raise_detailed_error(request_object)
        return request_object.json()

    async def revoke_refresh_tokens(self, uid):
        return auth.revoke_refresh_tokens(uid)


if __name__ == "__main__":
    authentication = Authentication()
    asyncio.run(authentication.make_default("waiz.zeeshan@camp1.tkxel.com"))

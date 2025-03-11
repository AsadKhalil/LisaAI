import logging
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.src.view import router
from app.src.authview import auth_router
from app.src.constants import FIREBASE_API_KEY, GOOGLE_APPLICATION_CREDENTIALS, OPENAI_API_KEY
import uvicorn
import os
from dotenv import load_dotenv

app = FastAPI()
app.include_router(router)
app.include_router(auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger_level = os.environ.get("log")

level = logging.DEBUG if logger_level == "debug" else logging.INFO
logging.basicConfig(stream=sys.stdout, level=level,
                    format='%(asctime)s %(levelname)s: %(name)s: %(funcName)s: %(message)s')

logger = logging.getLogger("main")


def check_environment(env_variables, logger: logging.Logger):
    for env_var in env_variables:
        if os.getenv(env_var) is None or os.getenv(env_var) == "":
            logger.critical(f"{env_var} is not set")
            exit(1)
        else:
            logger.info(f"{env_var} is set as {os.getenv(env_var)}")


@app.on_event("startup")
async def startup_event():
    logger = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)


if __name__ == '__main__':
    load_dotenv()

    current_dir = os.getcwd()

    logging.info(f"current root directory: {current_dir}")

    # google_application_credentials = os.path.join(
    #     current_dir, "firebase_creds.json")
    # os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = google_application_credentials
    print(current_dir)
    # check_environment(
    #     env_variables=[OPENAI_API_KEY, GOOGLE_APPLICATION_CREDENTIALS, FIREBASE_API_KEY], logger=logging)

    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["disable_existing_loggers"] = False

    log_config['loggers']['fastapi'] = {
        'handlers': ['default'],
        'level': 'INFO'
    }

    environment = os.getenv("environment")
    if environment == "production":
        uvicorn.run(app="app.main:app", host='0.0.0.0',
                    port=9000, log_config=log_config, workers=3, proxy_headers=True)
    elif environment == "development":
        uvicorn.run(app="app.main:app", host='0.0.0.0',
                    port=9000, log_config=log_config, workers=2)
    else:
        uvicorn.run(app="app.main:app", host='0.0.0.0', port=9000,
                    reload=True, log_config=log_config)

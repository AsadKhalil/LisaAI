import os
import re
import shutil
import sys
import traceback
from click import FileError
from fastapi import HTTPException
from pypdf import PdfReader
from tqdm import tqdm
import app.src.constants as constants
from langchain.docstore.document import Document
from app.src.modules.aws import AWS
from app.src.modules.databases import ConversationDB, PGVectorManager
import pymupdf4llm
import urllib.parse


async def new_knowledge_base(files):
    """create a new rag database from uploaded files"""
    try:

        # clear knowledge base files directory
        save_path = os.path.join(os.getcwd(), constants.UPLOAD_PATH)
        if os.path.exists(save_path):
            shutil.rmtree(save_path)
        os.mkdir(save_path)

        filepaths = []
        data = []
        for file in files:
            file_path = os.path.join(save_path, file.filename)
            try:
                # First, upload it to a specific folder regardless of its format
                with open(file_path, "wb") as buffer:
                    buffer.write(file.file.read())
                aws = AWS()
                url = aws.upload_file_path_to_s3(file_path, file.filename)
                filepaths.append(
                    {"file_path": file_path, "filename": file.filename, "url": url})
                data.append({"filename": file.filename, "url": url})
            except FileError:
                print("Error in save file, create knowledge base")
                print(traceback.format_exc())
                print(sys.exc_info()[2])

        for file in filepaths:
            await ingest_file(file)

        shutil.rmtree(constants.UPLOAD_PATH)
        return data
    except Exception as e:
        shutil.rmtree(constants.UPLOAD_PATH)
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))


async def load_pdf(filepath):
    docs = []
    md_texts = pymupdf4llm.to_markdown(
        filepath, write_images=True, page_chunks=True, image_path=constants.IMAGES_DIRECTORY)

    images = {}
    await upload_image(constants.IMAGES_DIRECTORY, images)

    if os.path.exists(constants.IMAGES_DIRECTORY):
        shutil.rmtree(constants.IMAGES_DIRECTORY)

    for index, page in enumerate(tqdm(md_texts)):

        text = page["text"]
        pattern = r"\[.*?\]\(([^)]+\.png)\)"
        matches = re.findall(pattern, text)

        for match in matches:
            url = images[match]
            text = text.replace(match, url)
            page["text"] = text

        doc = Document(page_content=page["text"], metadata={
            "source": filepath.split('/')[-1], "page": index+1
        })
        docs.append(doc)

    return docs


async def upload_image(dir_path, image_hash):
    aws = AWS()
    for filename in tqdm(os.listdir(dir_path)):
        file_path = os.path.join(dir_path, filename)
        image_link = aws.upload_file_path_to_s3(file_path, file_path)
        image_hash[file_path] = image_link


async def ingest_file(file):
    """ingest a file into the knowledge base"""

    filepath = file["file_path"]
    url = file["url"]
    docs = await load_pdf(filepath)

    for i, d in enumerate(tqdm(docs)):
        d.metadata["url"] = urllib.parse.quote(
            url) + "#page=" + str(d.metadata["page"])
        text = '{ content: "' + d.page_content + '"}' + str(d.metadata)

        # Remove control characters:
        cleaned_text = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', text)

        d.page_content = cleaned_text

    VECTORSTORE_COLLECTION_NAME = os.environ.get("VECTORSTORE_COLLECTION_NAME")

    vectorstoremanager = PGVectorManager()
    vectorstore = vectorstoremanager.return_vector_store(
        VECTORSTORE_COLLECTION_NAME, True)
    await vectorstore.aadd_documents(docs)
    vectorstoremanager.close()

import os
import re
import sys
import shutil
import traceback
import urllib.parse
import logging
import asyncio

from fastapi import HTTPException, UploadFile
from tqdm import tqdm
from langchain.docstore.document import Document
import pymupdf4llm

from app.src.constants import UPLOAD_PATH, IMAGES_DIRECTORY
import app.src.constants as constants
from app.src.modules.aws import AWS
from app.src.modules.databases import PGVectorManager

logger = logging.getLogger("knowledge_base")


async def new_knowledge_base(files):
    """create a new rag database from uploaded files"""
    try:
        # Get the absolute path to the app directory
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # clear knowledge base files directory
        save_path = os.path.join(app_dir, constants.UPLOAD_PATH)
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

        #shutil.rmtree(save_path)
        return data
    except Exception as e:
        # if os.path.exists(save_path):
        #     shutil.rmtree(save_path)
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


async def load_text_file(filepath):
    """Load content from a text file and split into chunks based on Q&A format"""
    docs = []
    try:
        with open(filepath, 'r', encoding='utf-8') as file:
            text = file.read()
            
            # Split the text into chunks based on "Question:" markers
            chunks = text.split("Question:")
            
            for i, chunk in enumerate(chunks[1:], 1):  # Skip the first empty chunk
                # Split the chunk into question and answer
                parts = chunk.split("Answer:", 1)
                if len(parts) == 2:
                    question = parts[0].strip()
                    answer = parts[1].strip()
                    
                    # Create a document for this Q&A pair
                    doc = Document(
                        page_content=f"Question: {question}\nAnswer: {answer}",
                        metadata={
                            "source": filepath.split('/')[-1],
                            "page": i,
                            "question": question
                        }
                    )
                    docs.append(doc)
                else:
                    # If the chunk doesn't have a clear Q&A format, create a document for the whole chunk
                    doc = Document(
                        page_content=chunk.strip(),
                        metadata={
                            "source": filepath.split('/')[-1],
                            "page": i
                        }
                    )
                    docs.append(doc)
                    
    except Exception as e:
        print(f"Error reading text file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error reading text file: {str(e)}")
    return docs


async def ingest_file(file, collection_name=None):
    """ingest a file into the knowledge base"""
    filepath = file["file_path"]
    url = file["url"]
    
    # Check file extension and load accordingly
    if filepath.lower().endswith('.txt'):
        docs = await load_text_file(filepath)
    else:
        docs = await load_pdf(filepath)

    for i, d in enumerate(tqdm(docs)):
        d.metadata["url"] = urllib.parse.quote(
            url) + "#page=" + str(d.metadata["page"])
        d.metadata["collection_name"] = collection_name  # Ensure collection_name is in metadata
        text = '{ content: "' + d.page_content + '"}' + str(d.metadata)

        # Remove control characters:
        cleaned_text = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', text)

        d.page_content = cleaned_text

    # Use the provided collection name or default to drug-index for drug-related files
    if collection_name is None:
        if "drug" in filepath.lower() or "medical" in filepath.lower() or "health" in filepath.lower():
            collection_name = "drug-index"
        else:
            collection_name = os.environ.get("VECTORSTORE_COLLECTION_NAME")

    vectorstoremanager = PGVectorManager()
    vectorstore = vectorstoremanager.return_vector_store(
        collection_name, True)
    logger.info(f"Adding documents to {collection_name} for {filepath}")
    logger.info(f"Vector store created successfully, attempting to add {len(docs)} documents")
    try:
        # Process documents in batches of 50
        batch_size = 50
        for i in range(0, len(docs), batch_size):
            batch = docs[i:i + batch_size]
            logger.info(f"Processing batch {i//batch_size + 1} of {(len(docs) + batch_size - 1)//batch_size}")
            try:
                # Add timeout to the operation
                await asyncio.wait_for(
                    vectorstore.aadd_documents(batch),
                    timeout=30  # 30 second timeout
                )
                logger.info(f"Successfully added batch {i//batch_size + 1}")
            except asyncio.TimeoutError:
                logger.error(f"Timeout while processing batch {i//batch_size + 1}")
                raise Exception(f"Timeout while processing batch {i//batch_size + 1}")
            except Exception as e:
                logger.error(f"Error processing batch {i//batch_size + 1}: {str(e)}")
                logger.error(f"First document in failed batch: {batch[0].page_content[:200]}...")
                raise
        
        logger.info(f"Successfully added all documents to {collection_name} for {filepath}")
    except Exception as e:
        logger.error(f"Error adding documents: {str(e)}")
        logger.error(traceback.format_exc())
        raise
    finally:
        vectorstoremanager.close()


async def create_drug_index(files):
    """create or update drug information index from uploaded files"""
    try:
        # Get the absolute path to the app directory
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        print("#####App dir: ",app_dir)
        # Create or use existing drug index files directory
        save_path = os.path.join(app_dir, constants.UPLOAD_PATH, "drug_index")
        os.makedirs(save_path, exist_ok=True)  # Create directory if it doesn't exist, don't clear it

        filepaths = []
        data = []
        for file in files:
            file_path = os.path.join(save_path, file.filename)
            try:
                # Upload file to the drug index directory
                with open(file_path, "wb") as buffer:
                    buffer.write(file.file.read())
                aws = AWS()
                url = aws.upload_file_path_to_s3(file_path, file.filename)
                filepaths.append(
                    {"file_path": file_path, "filename": file.filename, "url": url})
                data.append({"filename": file.filename, "url": url})
            except FileError:
                print("Error in save file, create drug index")
                print(traceback.format_exc())
                print(sys.exc_info()[2])

        for file in filepaths:
            await ingest_file(file, collection_name="drug-index")

        return data
    except Exception as e:
        print(traceback.format_exc())
        print(sys.exc_info()[2])
        raise HTTPException(status_code=500, detail=str(e))

def smart_chunk_text(text: str, target_size: int = 500, overlap: int = 50):
    """Split text into chunks based on target size and overlap"""
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + target_size, len(words))
        chunk = ' '.join(words[start:end])
        chunks.append(chunk)
        start = end - overlap
    return chunks


async def parse_text_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()


async def parse_pdf_file(filepath):
    markdown_pages = pymupdf4llm.to_markdown(
        filepath, write_images=True, page_chunks=True, image_path=IMAGES_DIRECTORY
    )
    images = {}
    aws = AWS()
    for img in os.listdir(IMAGES_DIRECTORY):
        img_path = os.path.join(IMAGES_DIRECTORY, img)
        images[img_path] = aws.upload_file_path_to_s3(img_path, img_path)

    shutil.rmtree(IMAGES_DIRECTORY)

    text = ""
    for page in markdown_pages:
        page_text = page["text"]
        for match in re.findall(r"\[.*?\]\(([^)]+\.png)\)", page_text):
            page_text = page_text.replace(match, images.get(match, match))
        text += page_text + "\n"
    return text


async def process_file(file: UploadFile, collection_name: str = "drug-index"):
    """ingest a file into the knowledge base"""
    try:
        logger.info(f"Starting to process file: {file.filename}")
        
        # Define save path
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_dir = os.path.join(app_dir, UPLOAD_PATH, collection_name)
        os.makedirs(save_dir, exist_ok=True)

        # Save file locally
        file_path = os.path.join(save_dir, file.filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())

        # Upload to S3
        logger.info(f"Uploading {file.filename} to S3")
        aws = AWS()
        url = aws.upload_file_path_to_s3(file_path, file.filename)
        logger.info(f"Successfully uploaded {file.filename} to S3")
        
        # Check file extension and load accordingly
        logger.info(f"Loading file content for: {file.filename}")
        if file.filename.lower().endswith('.txt'):
            docs = await load_text_file(file_path)
        else:
            docs = await load_pdf(file_path)
        logger.info(f"Successfully loaded {len(docs)} documents from {file.filename}")

        logger.info(f"Processing metadata for {file.filename}")
        for i, d in enumerate(docs):
            d.metadata["url"] = urllib.parse.quote(
                url) + "#page=" + str(d.metadata["page"])
            d.metadata["collection_name"] = collection_name  # Ensure collection_name is in metadata
            text = '{ content: "' + d.page_content + '"}' + str(d.metadata)

            # Remove control characters:
            cleaned_text = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', text)
            d.page_content = cleaned_text

        logger.info(f"Adding documents to {collection_name} for {file.filename}")
        vectorstoremanager = PGVectorManager()
        vectorstore = vectorstoremanager.return_vector_store(
            collection_name, True)
        
        # Debug logging for documents
        logger.info(f"Number of documents to add: {len(docs)}")
        for i, doc in enumerate(docs[:2]):  # Log first 2 documents as sample
            logger.info(f"Document {i} content length: {len(doc.page_content)}")
            logger.info(f"Document {i} metadata: {doc.metadata}")
        
        logger.info(f"Vector store created successfully, attempting to add {len(docs)} documents")
        try:
            # Process documents in batches of 50
            batch_size = 50
            for i in range(0, len(docs), batch_size):
                batch = docs[i:i + batch_size]
                logger.info(f"Processing batch {i//batch_size + 1} of {(len(docs) + batch_size - 1)//batch_size}")
                try:
                    # Add timeout to the operation
                    await asyncio.wait_for(
                        vectorstore.aadd_documents(batch),
                        timeout=30  # 30 second timeout
                    )
                    logger.info(f"Successfully added batch {i//batch_size + 1}")
                except asyncio.TimeoutError:
                    logger.error(f"Timeout while processing batch {i//batch_size + 1}")
                    raise Exception(f"Timeout while processing batch {i//batch_size + 1}")
                except Exception as e:
                    logger.error(f"Error processing batch {i//batch_size + 1}: {str(e)}")
                    logger.error(f"First document in failed batch: {batch[0].page_content[:200]}...")
                    raise
            
            logger.info(f"Successfully added all documents to {collection_name} for {file.filename}")
        except Exception as e:
            logger.error(f"Error adding documents: {str(e)}")
            logger.error(traceback.format_exc())
            raise
        finally:
            vectorstoremanager.close()
        
        # Clean up local file
        if os.path.exists(file_path):
            os.remove(file_path)
            
        logger.info(f"Completed processing file: {file.filename}")
        
        # Add file to database
        from app.src.modules.databases import ConversationDB
        db = ConversationDB()
        await db.add_files([{"filename": file.filename, "url": url}], user_id=1)
        
        return {"filename": file.filename, "status": "success", "url": url}
    except Exception as e:
        logger.error(f"Error processing file {file.filename}: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

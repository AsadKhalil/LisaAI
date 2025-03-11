from fastapi import HTTPException
from pydantic import BaseModel
from app.src.modules.databases import ConversationDB
from app.src.data_types import Rating


async def add_rating_data(data: Rating, db: ConversationDB):
    if data.query_id is None:
        raise HTTPException(
            status_code=400, detail="query_id must be provided")

    await db.insert_review_and_rating(
        query_id=data.query_id, review=data.review, rating=data.rating)

    return "rating added"


async def add_hr_rating_data(data: Rating, db: ConversationDB):
    if data.query_id is None:
        raise HTTPException(
            status_code=400, detail="query_id must be provided")

    if data.rating is None:
        await db.insert_hr_review_and_rating(
            query_id=data.query_id, review=data.review, rating=None)
    if data.review is None or data.review == "":
        await db.insert_hr_review_and_rating(
            query_id=data.query_id, review=None, rating=data.rating)

    return "rating added"

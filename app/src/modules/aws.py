import os
import boto3
from typing import Dict
from fastapi import UploadFile, HTTPException
import urllib.parse


class AWS:
    """Class used for AWS services"""

    def __init__(self) -> None:
        """Initialize the AWS client with credentials from the environment variables."""
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
            region_name=os.getenv('AWS_REGION')
        )
        self.bucket_name = os.getenv('AWS_BUCKET_NAME')

    def generate_file_urls(self):
        data = []
        urls = {}
        paginator = self.s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket_name):
            for obj in page.get('Contents', []):
                url = f"https://{self.bucket_name}.s3.amazonaws.com/{obj['Key']}"
                urls[obj['Key']] = url
                data.append({"filename": obj['Key'], "url": url})
        return data

    def delete_file(self, file_key):
        """Delete a file from the S3 bucket.

        Args:
            file_key (str): The key (file name) of the file to delete in the S3 bucket.

        Returns:
            response (dict): Response from the S3 delete operation.
        """
        try:
            response = self.s3_client.delete_object(
                Bucket=self.bucket_name, Key=file_key)
            return response
        except Exception as e:
            print(f"An error occurred: {e}")
            return None

    def upload_file_to_s3(self, file: UploadFile, file_name: str) -> str:
        """Uploads file to S3 bucket with public-read ACL."""
        try:
            self.s3_client.upload_fileobj(
                Fileobj=file.file,
                Bucket=self.bucket_name,
                Key=file_name,
                ExtraArgs={
                    "ACL": "public-read"
                }
            )
            return f"https://{self.bucket_name}.s3.amazonaws.com/{file_name}"
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    def upload_file_path_to_s3(self, file_path: str, file_name: str) -> str:
        """Uploads file to S3 bucket with public-read ACL."""
        try:
            with open(file_path, 'rb') as file:
                if file_path.endswith('.pdf'):
                    self.s3_client.upload_fileobj(
                        Fileobj=file,
                        Bucket=self.bucket_name,
                        Key=file_name,
                        ExtraArgs={
                            "ACL": "public-read",
                            "ContentType": "application/pdf",
                            "ContentDisposition": "inline; filename={}".format(file_name)
                        }
                    )
                if file_path.endswith('.docx'):
                    self.s3_client.upload_fileobj(
                        Fileobj=file,
                        Bucket=self.bucket_name,
                        Key=file_name,
                        ExtraArgs={
                            "ACL": "public-read",
                            "ContentType": "application/docx",
                            "ContentDisposition": "inline; filename={}".format(file_name)
                        }
                    )
                if file_path.endswith('.png'):
                    self.s3_client.upload_fileobj(
                        Fileobj=file,
                        Bucket=self.bucket_name,
                        Key=file_name,
                        ExtraArgs={
                            "ACL": "public-read",
                            "ContentType": "image/png",
                            "ContentDisposition": "inline; filename={}".format(file_name)
                        }
                    )
                    file_name_encoded = urllib.parse.quote(file_name)
                    return f"https://{self.bucket_name}.s3.amazonaws.com/{file_name_encoded}"
            return f"https://{self.bucket_name}.s3.amazonaws.com/{file_name}"
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

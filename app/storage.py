import boto3
import os

from fastapi.responses import StreamingResponse
from io import BytesIO
from dotenv import load_dotenv

load_dotenv()

R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto"
)

def upload_file(file, filename, folder="documents"):

    file_key = f"{folder}/{filename}"

    s3.upload_fileobj(
        file.file,
        R2_BUCKET_NAME,
        file_key,
    )

    return file_key

def get_file_url(file_key):

    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET_NAME,
            "Key": file_key
        },
        ExpiresIn=3600
    )

    return url

def download_file(file_key):

    response = s3.get_object(
        Bucket=R2_BUCKET_NAME,
        Key=file_key
    )

    return response
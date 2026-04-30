import os
import logging
import mimetypes
import boto3
from botocore.config import Config
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _get_s3_client():
    config = Config(region_name=settings.aws_s3_region)
    kwargs = {
        "aws_access_key_id": settings.aws_access_key_id,
        "aws_secret_access_key": settings.aws_secret_access_key,
        "config": config,
    }
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url

    return boto3.client("s3", **kwargs)


def upload_to_s3(local_path: str, s3_key: str) -> str:
    """
    Upload a file to S3/R2 and return the public URL.

    Args:
        local_path: Path to the local file.
        s3_key: The S3 object key (e.g., "clips/uuid.mp4").

    Returns:
        The URL of the uploaded object.
    """
    client = _get_s3_client()

    logger.info(f"Uploading {local_path} to s3://{settings.aws_s3_bucket}/{s3_key}")

    content_type, _ = mimetypes.guess_type(local_path)
    if not content_type:
        content_type = "application/octet-stream"

    client.upload_file(
        local_path,
        settings.aws_s3_bucket,
        s3_key,
        ExtraArgs={
            "ContentType": content_type,
        },
    )

    if settings.s3_endpoint_url:
        url = f"{settings.s3_endpoint_url}/{settings.aws_s3_bucket}/{s3_key}"
    else:
        url = f"https://{settings.aws_s3_bucket}.s3.{settings.aws_s3_region}.amazonaws.com/{s3_key}"

    logger.info(f"Upload complete: {url}")
    return url


def generate_presigned_url(s3_key: str, expiration: int = 3600) -> str:
    """Generate a presigned URL for downloading a clip."""
    client = _get_s3_client()

    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.aws_s3_bucket,
            "Key": s3_key,
        },
        ExpiresIn=expiration,
    )
    return url


def check_s3_object_exists(s3_key: str) -> bool:
    """
    Check if an object exists in S3 using HEAD request.
    
    Args:
        s3_key: The S3 object key to check.
        
    Returns:
        True if the object exists, False otherwise.
    """
    client = _get_s3_client()
    
    try:
        client.head_object(Bucket=settings.aws_s3_bucket, Key=s3_key)
        logger.info(f"S3 object exists: s3://{settings.aws_s3_bucket}/{s3_key}")
        return True
    except client.exceptions.ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "404":
            logger.info(f"S3 object not found: s3://{settings.aws_s3_bucket}/{s3_key}")
            return False
        # For other errors (permissions, etc.), log and return False
        logger.warning(f"Error checking S3 object {s3_key}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Unexpected error checking S3 object {s3_key}: {e}")
        return False


def get_s3_url(s3_key: str) -> str:
    """
    Generate the public S3 URL for a given key.
    
    Args:
        s3_key: The S3 object key.
        
    Returns:
        The public URL of the S3 object.
    """
    if settings.s3_endpoint_url:
        return f"{settings.s3_endpoint_url}/{settings.aws_s3_bucket}/{s3_key}"
    else:
        return f"https://{settings.aws_s3_bucket}.s3.{settings.aws_s3_region}.amazonaws.com/{s3_key}"


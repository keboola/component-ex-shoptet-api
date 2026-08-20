import logging

from keboola.component.exceptions import UserException
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


class Configuration(BaseModel):
    print_hello: bool
    api_token: str = Field(alias="#api_token")
    debug: bool = False

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(error_messages)}")

        if self.debug:
            logger.debug("Component will run in Debug mode")

    @field_validator("api_token")
    def token_must_be_uppercase(cls, v):
        if not v.isupper():
            raise UserException("API token must be uppercase")
        return v

from fastapi import HTTPException


class ApiError(HTTPException):
    """HTTP error with a stable machine-readable `code` the frontend can switch on."""

    def __init__(self, status_code: int, code: str, message: str, headers: dict | None = None):
        super().__init__(status_code, detail={"code": code, "message": message}, headers=headers)

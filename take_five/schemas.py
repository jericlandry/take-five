from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class CreatePersonRequest(BaseModel):
    name: str
    p_type: str
    phone: Optional[str] = None
    email: Optional[str] = None
    aliases: Optional[List[str]] = None
    notes: Optional[str] = None
    external_id: Optional[str] = None
    date_of_birth: Optional[str] = None


class UpdatePersonRequest(BaseModel):
    name: Optional[str] = None
    p_type: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    aliases: Optional[List[str]] = None
    notes: Optional[str] = None
    external_id: Optional[str] = None
    date_of_birth: Optional[str] = None


class CreateCareCircleRequest(BaseModel):
    name: str
    status: str = 'active'
    external_id: Optional[str] = None


class CreateCircleMembershipRequest(BaseModel):
    role: str  # senior | family | caregiver | professional


class UpdateCareCircleRequest(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    external_id: Optional[str] = None
    integration_config: Optional[dict] = None


class CreateEnsembleRequest(BaseModel):
    name: str
    plan: str
    status: str = "trial"


class UpdateClinicalRecordRequest(BaseModel):
    data: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class CreateClinicalRecordRequest(BaseModel):
    person_id: str
    resource_type: str
    data: Dict[str, Any]
    notes: Optional[str] = None
    status: str = 'active'


class UpdateEnsembleMembershipRequest(BaseModel):
    ensemble_id: str
    user_role: str  # 'admin' | 'member'


class MessageRequest(BaseModel):
    circle_id: str
    message: str
    response_format: str = "markdown"


class DigestRequest(BaseModel):
    circle_id: str
    response_format: str = "markdown"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None

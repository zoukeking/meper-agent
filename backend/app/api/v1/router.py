"""V1 API router aggregator."""
from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.agents import router as agents_router
from app.api.v1.api_keys import router as api_keys_router
from app.api.v1.auth import router as auth_router
from app.api.v1.channels import inbound_router as channels_inbound_router
from app.api.v1.channels import router as channels_router
from app.api.v1.credentials import router as credentials_router
from app.api.v1.ext import router as ext_router
from app.api.v1.files import router as files_router
from app.api.v1.health import router as health_router
from app.api.v1.knowledge_bases import router as knowledge_bases_router
from app.api.v1.mcp import router as mcp_router
from app.api.v1.models import router as models_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.roles import router as roles_router
from app.api.v1.sessions import router as sessions_router
from app.api.v1.tasks import router as tasks_router
from app.api.v1.tools import router as tools_router
from app.api.v1.triggers import router as triggers_router
from app.api.v1.webhooks import router as webhooks_router
from app.api.v1.workflow_registry import router as workflow_registry_router
from app.api.v1.workflows import router as workflows_router
from app.api.v1.ws import router as ws_router

api_v1_router = APIRouter()
api_v1_router.include_router(health_router)
api_v1_router.include_router(auth_router)
api_v1_router.include_router(admin_router)
api_v1_router.include_router(agents_router)
api_v1_router.include_router(credentials_router)
api_v1_router.include_router(api_keys_router)
api_v1_router.include_router(ext_router)
api_v1_router.include_router(files_router)
api_v1_router.include_router(knowledge_bases_router)
api_v1_router.include_router(models_router)
api_v1_router.include_router(notifications_router)
api_v1_router.include_router(sessions_router)
api_v1_router.include_router(tasks_router)
api_v1_router.include_router(tools_router)
api_v1_router.include_router(triggers_router)
api_v1_router.include_router(mcp_router)
api_v1_router.include_router(roles_router)
api_v1_router.include_router(workflow_registry_router)
api_v1_router.include_router(workflows_router)
api_v1_router.include_router(webhooks_router)
api_v1_router.include_router(channels_router)
api_v1_router.include_router(channels_inbound_router)
api_v1_router.include_router(ws_router)

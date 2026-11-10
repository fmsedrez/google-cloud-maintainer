#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Dict, Iterable, List, Optional

import google.auth
from google.api_core import client_options as client_options_lib
from google.api_core import exceptions as gcp_exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import api_keys_v2
from google.cloud import cloudquotas_v1
from google.protobuf import field_mask_pb2
import requests

# ==============================================================================
# VARIÁVEIS CONFIGURÁVEIS CENTRALIZADAS (SOBRESCREVÍVEIS VIA VARIÁVEIS DE AMBIENTE)
# ==============================================================================
PROJECT_ID: str = os.getenv("GCP_PROJECT_ID", "meu-projeto-vertex-isolado")
PROJECT_NUMBER: str = os.getenv("GCP_PROJECT_NUMBER", "123456789012")
BILLING_ACCOUNT_ID: str = os.getenv("GCP_BILLING_ACCOUNT_ID", "012345-6789AB-CDEF01")
REGION: str = os.getenv("GCP_REGION", "us-central1")
ALLOWED_MODEL: str = os.getenv("VERTEX_ALLOWED_MODEL", "gemini-1.5-pro")
ALLOWED_MODELS: List[str] = [
    m.strip()
    for m in os.getenv("VERTEX_ALLOWED_MODELS", ALLOWED_MODEL).split(",")
    if m.strip()
]
BLOCKED_MODELS: List[str] = [
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "claude-3-5-sonnet",
    "claude-3-5-sonnet-v2",
    "claude-3-7-sonnet",
    "claude-3-opus",
    "claude-3-haiku",
    "claude-3-5-haiku",
    "llama-3.1-405b-instruct-maas",
    "llama-3.1-70b-instruct-maas",
    "llama-3.3-70b-instruct-maas",
    "mistral-large",
    "codestral",
]
MONTHLY_BUDGET_USD: float = float(os.getenv("MONTHLY_BUDGET_USD", "50.0"))
BUDGET_RESOURCE_NAME: str = os.getenv(
    "BUDGET_RESOURCE_NAME",
    f"billingAccounts/{BILLING_ACCOUNT_ID}/budgets/SEU_BUDGET_ID",
)
ALLOWED_RPM_LIMIT: int = int(os.getenv("VERTEX_ALLOWED_RPM", "60"))
API_KEY_ID: str = os.getenv("VERTEX_API_KEY_ID", "vertex-isolated-key")
API_KEY_DISPLAY_NAME: str = os.getenv(
    "VERTEX_API_KEY_DISPLAY_NAME", "Vertex AI Isolated Model Key"
)
CONTACT_EMAIL: str = os.getenv("GCP_QUOTA_CONTACT_EMAIL", "")

VERTEX_SERVICE_NAME: str = "aiplatform.googleapis.com"
INNOCUOUS_SERVICE_NAME: str = "cloudtrace.googleapis.com"
QUOTA_ID: str = "GenerateContentRequestsPerMinutePerProjectPerBaseModel"
BILLING_BUDGETS_BASE_URL: str = "https://billingbudgets.googleapis.com/v1"
HTTP_TIMEOUT_SECONDS: int = 60

SAFETY_CHECKS_TO_IGNORE: List[cloudquotas_v1.QuotaSafetyCheck] = [
    cloudquotas_v1.QuotaSafetyCheck.QUOTA_DECREASE_BELOW_USAGE,
    cloudquotas_v1.QuotaSafetyCheck.QUOTA_DECREASE_PERCENTAGE_TOO_HIGH,
]
VALID_ACTIONS = ("enable", "disable", "lift-cap")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("toggle_access")


def validate_configuration(require_budget: bool) -> None:
    if not PROJECT_ID or PROJECT_ID == "meu-projeto-vertex-isolado":
        logger.error("Configure PROJECT_ID / GCP_PROJECT_ID antes de executar.")
        sys.exit(1)
    if require_budget and (
        not BUDGET_RESOURCE_NAME
        or "SEU_BUDGET_ID" in BUDGET_RESOURCE_NAME
        or "012345-6789AB-CDEF01" in BUDGET_RESOURCE_NAME
    ):
        logger.error("Configure BUDGET_RESOURCE_NAME com o recurso retornado por setup_option1.py.")
        sys.exit(1)


def build_preference_id(region: str, model_name: str) -> str:
    raw = f"vertex-rpm-{region}-{model_name}".lower()
    sanitized = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    return sanitized[:63].rstrip("-")


def resolve_api_key_resource_name(
    client: api_keys_v2.ApiKeysClient,
    project_id: str,
    key_id: str,
    display_name: str,
) -> str:
    parent = f"projects/{project_id}/locations/global"
    candidate_name = f"{parent}/keys/{key_id}"
    try:
        return client.get_key(request=api_keys_v2.GetKeyRequest(name=candidate_name)).name
    except gcp_exceptions.NotFound:
        for key_obj in client.list_keys(request=api_keys_v2.ListKeysRequest(parent=parent)):
            if key_obj.display_name == display_name:
                return key_obj.name
    raise RuntimeError(f"API Key '{key_id}' não encontrada em {parent}.")


def update_api_key_target_service(project_id: str, target_service: str) -> api_keys_v2.Key:
    client = api_keys_v2.ApiKeysClient(
        client_options=client_options_lib.ClientOptions(quota_project_id=project_id)
    )
    key_name = resolve_api_key_resource_name(
        client=client,
        project_id=project_id,
        key_id=API_KEY_ID,
        display_name=API_KEY_DISPLAY_NAME,
    )
    updated_key = api_keys_v2.Key(
        name=key_name,
        restrictions=api_keys_v2.Restrictions(
            api_targets=[api_keys_v2.ApiTarget(service=target_service)]
        ),
    )
    update_request = api_keys_v2.UpdateKeyRequest(
        key=updated_key,
        update_mask=field_mask_pb2.FieldMask(paths=["restrictions"]),
    )
    return client.update_key(request=update_request).result(timeout=120)


def find_existing_preference_name(
    client: cloudquotas_v1.CloudQuotasClient,
    parent: str,
    region: str,
    model_name: str,
) -> Optional[str]:
    for pref in client.list_quota_preferences(
        request=cloudquotas_v1.ListQuotaPreferencesRequest(parent=parent)
    ):
        if (
            pref.service == VERTEX_SERVICE_NAME
            and pref.quota_id == QUOTA_ID
            and pref.dimensions.get("region") == region
            and pref.dimensions.get("base_model") == model_name
        ):
            return pref.name
    return None


def set_models_rpm_quota(
    project_id: str,
    region: str,
    models: Iterable[str],
    preferred_rpm: int,
    justification: str,
) -> None:
    client = cloudquotas_v1.CloudQuotasClient(
        client_options=client_options_lib.ClientOptions(quota_project_id=project_id)
    )
    parent = f"projects/{project_id}/locations/global"

    for model_name in sorted({m.strip() for m in models if m.strip()}):
        existing_name = find_existing_preference_name(client, parent, region, model_name)
        pref_name = (
            existing_name
            if existing_name
            else f"{parent}/quotaPreferences/{build_preference_id(region, model_name)}"
        )
        preference = cloudquotas_v1.QuotaPreference(
            name=pref_name,
            service=VERTEX_SERVICE_NAME,
            quota_id=QUOTA_ID,
            dimensions={"region": region, "base_model": model_name},
            quota_config=cloudquotas_v1.QuotaConfig(preferred_value=preferred_rpm),
            justification=justification,
        )
        if CONTACT_EMAIL:
            preference.contact_email = CONTACT_EMAIL

        update_request = cloudquotas_v1.UpdateQuotaPreferenceRequest(
            quota_preference=preference,
            update_mask=field_mask_pb2.FieldMask(paths=["quota_config.preferred_value"]),
            allow_missing=True,
            ignore_safety_checks=SAFETY_CHECKS_TO_IGNORE,
        )
        updated = client.update_quota_preference(request=update_request)
        logger.info(
            "QuotaPreference atualizada: base_model=%s | preferred=%d RPM | granted=%s",
            model_name,
            updated.quota_config.preferred_value,
            updated.quota_config.granted_value,
        )


def get_billing_auth_headers(quota_project_id: str) -> Dict[str, str]:
    credentials, _ = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/cloud-billing",
            "https://www.googleapis.com/auth/cloud-platform",
        ]
    )
    credentials.refresh(GoogleAuthRequest())
    return {
        "Authorization": f"Bearer {credentials.token}",
        "X-Goog-User-Project": quota_project_id,
        "Content-Type": "application/json",
    }


def patch_budget_spend_cap_state(
    budget_resource_name: str,
    quota_project_id: str,
    input_state: str,
) -> Dict[str, object]:
    headers = get_billing_auth_headers(quota_project_id=quota_project_id)
    patch_url = f"{BILLING_BUDGETS_BASE_URL}/{budget_resource_name}"
    params = {"updateMask": "spendCap.inputState"}
    body = {
        "name": budget_resource_name,
        "spendCap": {"inputState": input_state},
    }
    response = requests.patch(
        patch_url,
        headers=headers,
        params=params,
        data=json.dumps(body),
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if not response.ok:
        logger.error("Erro ao atualizar Spend Cap (HTTP %d): %s", response.status_code, response.text)
        response.raise_for_status()
    return response.json()


def handle_disable() -> None:
    validate_configuration(require_budget=False)
    update_api_key_target_service(project_id=PROJECT_ID, target_service=INNOCUOUS_SERVICE_NAME)
    set_models_rpm_quota(
        project_id=PROJECT_ID,
        region=REGION,
        models=ALLOWED_MODELS,
        preferred_rpm=0,
        justification="Acesso desabilitado operacionalmente via toggle_access.py disable.",
    )


def handle_enable() -> None:
    validate_configuration(require_budget=True)
    update_api_key_target_service(project_id=PROJECT_ID, target_service=VERTEX_SERVICE_NAME)
    set_models_rpm_quota(
        project_id=PROJECT_ID,
        region=REGION,
        models=ALLOWED_MODELS,
        preferred_rpm=ALLOWED_RPM_LIMIT,
        justification=f"Acesso reabilitado ({ALLOWED_RPM_LIMIT} RPM) via toggle_access.py enable.",
    )
    updated_budget = patch_budget_spend_cap_state(
        budget_resource_name=BUDGET_RESOURCE_NAME,
        quota_project_id=PROJECT_ID,
        input_state="CONFIGURED",
    )
    spend_cap = updated_budget.get("spendCap", {})
    if isinstance(spend_cap, dict) and spend_cap.get("outputState") == "ENFORCED":
        logger.warning(
            "Orçamento atingiu 100%% (outputState='ENFORCED'). Use 'lift-cap' para despausar neste ciclo."
        )


def handle_lift_cap() -> None:
    validate_configuration(require_budget=True)
    patch_budget_spend_cap_state(
        budget_resource_name=BUDGET_RESOURCE_NAME,
        quota_project_id=PROJECT_ID,
        input_state="AWAITING_NEXT_PERIOD",
    )


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in VALID_ACTIONS:
        print(f"Uso: python {os.path.basename(sys.argv[0])} <{'|'.join(VALID_ACTIONS)}>", file=sys.stderr)
        sys.exit(2)

    action = sys.argv[1]
    if action == "disable":
        handle_disable()
    elif action == "enable":
        handle_enable()
    elif action == "lift-cap":
        handle_lift_cap()


if __name__ == "__main__":
    main()

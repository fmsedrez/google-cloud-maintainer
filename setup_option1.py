#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Set, Tuple

import google.auth
from google.api_core import client_options as client_options_lib
from google.api_core import exceptions as gcp_exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import api_keys_v2
from google.cloud import cloudquotas_v1
from google.cloud import service_usage_v1
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
BUDGET_RESOURCE_NAME: str = os.getenv("BUDGET_RESOURCE_NAME", "")
ALLOWED_RPM_LIMIT: int = int(os.getenv("VERTEX_ALLOWED_RPM", "60"))
API_KEY_ID: str = os.getenv("VERTEX_API_KEY_ID", "vertex-isolated-key")
API_KEY_DISPLAY_NAME: str = os.getenv(
    "VERTEX_API_KEY_DISPLAY_NAME", "Vertex AI Isolated Model Key"
)
BUDGET_CURRENCY_CODE: str = os.getenv("BUDGET_CURRENCY_CODE", "USD")
CONTACT_EMAIL: str = os.getenv("GCP_QUOTA_CONTACT_EMAIL", "")

VERTEX_SERVICE_NAME: str = "aiplatform.googleapis.com"
VERTEX_BILLING_SERVICE_ID: str = "services/6F81-5844-456A"
QUOTA_ID: str = "GenerateContentRequestsPerMinutePerProjectPerBaseModel"
BILLING_BUDGETS_BASE_URL: str = "https://billingbudgets.googleapis.com/v1"
HTTP_TIMEOUT_SECONDS: int = 60

REQUIRED_APIS: List[str] = [
    "aiplatform.googleapis.com",
    "apikeys.googleapis.com",
    "cloudquotas.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudtrace.googleapis.com",
]

SAFETY_CHECKS_TO_IGNORE: List[cloudquotas_v1.QuotaSafetyCheck] = [
    cloudquotas_v1.QuotaSafetyCheck.QUOTA_DECREASE_BELOW_USAGE,
    cloudquotas_v1.QuotaSafetyCheck.QUOTA_DECREASE_PERCENTAGE_TOO_HIGH,
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("setup_option1")


def validate_configuration() -> None:
    placeholders = {
        "PROJECT_ID": (PROJECT_ID, "meu-projeto-vertex-isolado"),
        "PROJECT_NUMBER": (PROJECT_NUMBER, "123456789012"),
        "BILLING_ACCOUNT_ID": (BILLING_ACCOUNT_ID, "012345-6789AB-CDEF01"),
    }
    unconfigured = [
        name for name, (val, default) in placeholders.items() if not val or val == default
    ]
    if unconfigured:
        logger.error(
            "Variáveis obrigatórias ainda estão com valores de exemplo: %s. "
            "Edite o topo do arquivo ou exporte GCP_PROJECT_ID, GCP_PROJECT_NUMBER e GCP_BILLING_ACCOUNT_ID.",
            ", ".join(unconfigured),
        )
        sys.exit(1)
    if not PROJECT_NUMBER.isdigit():
        logger.error("PROJECT_NUMBER inválido ('%s'): deve conter apenas dígitos.", PROJECT_NUMBER)
        sys.exit(1)
    if MONTHLY_BUDGET_USD <= 0:
        logger.error("MONTHLY_BUDGET_USD deve ser maior que zero (atual: %s).", MONTHLY_BUDGET_USD)
        sys.exit(1)


def build_preference_id(region: str, model_name: str) -> str:
    raw = f"vertex-rpm-{region}-{model_name}".lower()
    sanitized = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    return sanitized[:63].rstrip("-")


def enable_required_apis(project_number: str, services: List[str]) -> None:
    logger.info("Habilitando APIs no projeto projects/%s: %s", project_number, ", ".join(services))
    client = service_usage_v1.ServiceUsageClient(
        client_options=client_options_lib.ClientOptions(quota_project_id=PROJECT_ID)
    )
    request = service_usage_v1.BatchEnableServicesRequest(
        parent=f"projects/{project_number}",
        service_ids=services,
    )
    operation = client.batch_enable_services(request=request)
    operation.result(timeout=300)
    logger.info("Todas as APIs requeridas foram habilitadas com sucesso.")


def provision_restricted_api_key(project_id: str, key_id: str) -> Tuple[str, str]:
    client = api_keys_v2.ApiKeysClient(
        client_options=client_options_lib.ClientOptions(quota_project_id=project_id)
    )
    parent = f"projects/{project_id}/locations/global"
    expected_name = f"{parent}/keys/{key_id}"
    restrictions = api_keys_v2.Restrictions(
        api_targets=[api_keys_v2.ApiTarget(service=VERTEX_SERVICE_NAME)]
    )

    try:
        logger.info("Criando API Key '%s' restrita a '%s'...", key_id, VERTEX_SERVICE_NAME)
        create_request = api_keys_v2.CreateKeyRequest(
            parent=parent,
            key_id=key_id,
            key=api_keys_v2.Key(
                display_name=API_KEY_DISPLAY_NAME,
                restrictions=restrictions,
            ),
        )
        operation = client.create_key(request=create_request)
        key_resource = operation.result(timeout=120)
        logger.info("API Key criada: %s (uid=%s)", key_resource.name, key_resource.uid)
    except gcp_exceptions.AlreadyExists:
        logger.info(
            "API Key '%s' já existe. Reconciliando restrições para '%s'...",
            expected_name,
            VERTEX_SERVICE_NAME,
        )
        update_request = api_keys_v2.UpdateKeyRequest(
            key=api_keys_v2.Key(
                name=expected_name,
                display_name=API_KEY_DISPLAY_NAME,
                restrictions=restrictions,
            ),
            update_mask=field_mask_pb2.FieldMask(paths=["display_name", "restrictions"]),
        )
        operation = client.update_key(request=update_request)
        key_resource = operation.result(timeout=120)

    key_string_response = client.get_key_string(
        request=api_keys_v2.GetKeyStringRequest(name=key_resource.name)
    )
    return key_resource.name, key_string_response.key_string


def list_existing_model_preferences(
    client: cloudquotas_v1.CloudQuotasClient,
    parent: str,
    region: str,
) -> Dict[str, str]:
    existing: Dict[str, str] = {}
    request = cloudquotas_v1.ListQuotaPreferencesRequest(parent=parent)
    for pref in client.list_quota_preferences(request=request):
        if (
            pref.service == VERTEX_SERVICE_NAME
            and pref.quota_id == QUOTA_ID
            and pref.dimensions.get("region") == region
            and "base_model" in pref.dimensions
        ):
            existing[pref.dimensions["base_model"]] = pref.name
    return existing


def discover_quota_models(
    client: cloudquotas_v1.CloudQuotasClient,
    project_id: str,
    region: str,
) -> Set[str]:
    discovered: Set[str] = set()
    quota_info_name = (
        f"projects/{project_id}/locations/global/services/"
        f"{VERTEX_SERVICE_NAME}/quotaInfos/{QUOTA_ID}"
    )
    try:
        info = client.get_quota_info(
            request=cloudquotas_v1.GetQuotaInfoRequest(name=quota_info_name)
        )
        for dim_info in info.dimensions_infos:
            dim_region = dim_info.dimensions.get("region")
            base_model = dim_info.dimensions.get("base_model")
            if base_model and (dim_region in (None, "", region)):
                discovered.add(base_model)
    except gcp_exceptions.GoogleAPICallError as exc:
        logger.warning(
            "Não foi possível consultar GetQuotaInfo (%s). Prosseguindo com BLOCKED_MODELS estático.",
            exc,
        )
    return discovered


def upsert_model_quota_preference(
    client: cloudquotas_v1.CloudQuotasClient,
    parent: str,
    existing_prefs: Dict[str, str],
    region: str,
    model_name: str,
    preferred_rpm: int,
    justification: str,
) -> cloudquotas_v1.QuotaPreference:
    dimensions = {"region": region, "base_model": model_name}
    quota_config = cloudquotas_v1.QuotaConfig(preferred_value=preferred_rpm)
    existing_name = existing_prefs.get(model_name)

    if existing_name:
        pref = cloudquotas_v1.QuotaPreference(
            name=existing_name,
            service=VERTEX_SERVICE_NAME,
            quota_id=QUOTA_ID,
            dimensions=dimensions,
            quota_config=quota_config,
            justification=justification,
        )
        if CONTACT_EMAIL:
            pref.contact_email = CONTACT_EMAIL
        update_req = cloudquotas_v1.UpdateQuotaPreferenceRequest(
            quota_preference=pref,
            update_mask=field_mask_pb2.FieldMask(paths=["quota_config.preferred_value"]),
            allow_missing=True,
            ignore_safety_checks=SAFETY_CHECKS_TO_IGNORE,
        )
        return client.update_quota_preference(request=update_req)

    pref_id = build_preference_id(region, model_name)
    pref = cloudquotas_v1.QuotaPreference(
        service=VERTEX_SERVICE_NAME,
        quota_id=QUOTA_ID,
        dimensions=dimensions,
        quota_config=quota_config,
        justification=justification,
    )
    if CONTACT_EMAIL:
        pref.contact_email = CONTACT_EMAIL

    try:
        create_req = cloudquotas_v1.CreateQuotaPreferenceRequest(
            parent=parent,
            quota_preference_id=pref_id,
            quota_preference=pref,
            ignore_safety_checks=SAFETY_CHECKS_TO_IGNORE,
        )
        return client.create_quota_preference(request=create_req)
    except gcp_exceptions.AlreadyExists:
        pref.name = f"{parent}/quotaPreferences/{pref_id}"
        update_req = cloudquotas_v1.UpdateQuotaPreferenceRequest(
            quota_preference=pref,
            update_mask=field_mask_pb2.FieldMask(paths=["quota_config.preferred_value"]),
            allow_missing=True,
            ignore_safety_checks=SAFETY_CHECKS_TO_IGNORE,
        )
        return client.update_quota_preference(request=update_req)


def configure_model_quotas(
    project_id: str,
    region: str,
    allowed_models: Iterable[str],
    blocked_models: Iterable[str],
    allowed_rpm: int,
) -> None:
    client = cloudquotas_v1.CloudQuotasClient(
        client_options=client_options_lib.ClientOptions(quota_project_id=project_id)
    )
    parent = f"projects/{project_id}/locations/global"
    allowed_set: Set[str] = {m.strip() for m in allowed_models if m.strip()}
    existing_prefs = list_existing_model_preferences(client, parent, region)
    discovered_models = discover_quota_models(client, project_id, region)
    models_to_block = sorted((set(blocked_models) | discovered_models) - allowed_set)

    for model in models_to_block:
        try:
            res = upsert_model_quota_preference(
                client=client,
                parent=parent,
                existing_prefs=existing_prefs,
                region=region,
                model_name=model,
                preferred_rpm=0,
                justification=f"Bloqueio de modelo não autorizado ({model}) no projeto isolado.",
            )
            logger.info("  [BLOQUEADO] base_model=%s | preferred=%d RPM | name=%s", model, res.quota_config.preferred_value, res.name)
        except gcp_exceptions.InvalidArgument as exc:
            logger.warning("  [IGNORADO] base_model='%s' não aplicável em '%s': %s", model, region, exc.message)

    for model in sorted(allowed_set):
        res = upsert_model_quota_preference(
            client=client,
            parent=parent,
            existing_prefs=existing_prefs,
            region=region,
            model_name=model,
            preferred_rpm=allowed_rpm,
            justification=f"Liberação controlada ({allowed_rpm} RPM) para modelo autorizado.",
        )
        logger.info(
            "  [PERMITIDO] base_model=%s | preferred=%d RPM | granted=%s | name=%s",
            model,
            res.quota_config.preferred_value,
            res.quota_config.granted_value,
            res.name,
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


def usd_to_money_dict(amount_usd: float, currency_code: str) -> Dict[str, object]:
    units = int(amount_usd)
    nanos = int(round((amount_usd - units) * 1_000_000_000))
    money: Dict[str, object] = {"units": str(units), "nanos": nanos}
    if currency_code:
        money["currencyCode"] = currency_code
    return money


def find_existing_budget_by_display_name(
    billing_account_id: str,
    display_name: str,
    headers: Dict[str, str],
) -> Optional[Dict[str, object]]:
    url = f"{BILLING_BUDGETS_BASE_URL}/billingAccounts/{billing_account_id}/budgets"
    page_token: Optional[str] = None
    while True:
        params: Dict[str, str] = {"pageSize": "100"}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
        for budget in payload.get("budgets", []):
            if budget.get("displayName") == display_name:
                return budget
        page_token = payload.get("nextPageToken")
        if not page_token:
            return None


def create_spend_cap_budget(
    billing_account_id: str,
    project_id: str,
    project_number: str,
    monthly_budget_usd: float,
) -> Dict[str, object]:
    headers = get_billing_auth_headers(quota_project_id=project_id)
    display_name = f"spend-cap-{project_id}-vertex-ai"

    if BUDGET_RESOURCE_NAME:
        get_url = f"{BILLING_BUDGETS_BASE_URL}/{BUDGET_RESOURCE_NAME}"
        existing_resp = requests.get(get_url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        if existing_resp.ok:
            return existing_resp.json()

    existing_budget = find_existing_budget_by_display_name(
        billing_account_id=billing_account_id,
        display_name=display_name,
        headers=headers,
    )
    if existing_budget:
        return existing_budget

    post_url = f"{BILLING_BUDGETS_BASE_URL}/billingAccounts/{billing_account_id}/budgets"
    budget_body = {
        "displayName": display_name,
        "budgetFilter": {
            "projects": [f"projects/{project_number}"],
            "services": [VERTEX_BILLING_SERVICE_ID],
            "calendarPeriod": "MONTH",
            "creditTypesTreatment": "EXCLUDE_ALL_CREDITS",
        },
        "amount": {
            "specifiedAmount": usd_to_money_dict(monthly_budget_usd, BUDGET_CURRENCY_CODE),
        },
        "thresholdRules": [
            {"thresholdPercent": 0.5, "spendBasis": "CURRENT_SPEND"},
            {"thresholdPercent": 0.8, "spendBasis": "CURRENT_SPEND"},
            {"thresholdPercent": 1.0, "spendBasis": "CURRENT_SPEND"},
        ],
        "notificationsRule": {
            "enableProjectLevelRecipients": True,
        },
        "ownershipScope": "ALL_USERS",
        "spendCap": {
            "inputState": "CONFIGURED",
        },
    }

    response = requests.post(
        post_url,
        headers=headers,
        data=json.dumps(budget_body),
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if not response.ok:
        logger.error("Falha ao criar orçamento Spend Cap (HTTP %d): %s", response.status_code, response.text)
        response.raise_for_status()
    return response.json()


def main() -> None:
    validate_configuration()
    enable_required_apis(project_number=PROJECT_NUMBER, services=REQUIRED_APIS)
    key_resource_name, raw_key_string = provision_restricted_api_key(
        project_id=PROJECT_ID,
        key_id=API_KEY_ID,
    )
    configure_model_quotas(
        project_id=PROJECT_ID,
        region=REGION,
        allowed_models=ALLOWED_MODELS,
        blocked_models=BLOCKED_MODELS,
        allowed_rpm=ALLOWED_RPM_LIMIT,
    )
    budget = create_spend_cap_budget(
        billing_account_id=BILLING_ACCOUNT_ID,
        project_id=PROJECT_ID,
        project_number=PROJECT_NUMBER,
        monthly_budget_usd=MONTHLY_BUDGET_USD,
    )
    budget_name = budget.get("name", "")

    print(f"API Key Resource Name : {key_resource_name}")
    print(f"API Key Raw String    : {raw_key_string}")
    print(f"Budget Resource Name  : {budget_name}")


if __name__ == "__main__":
    main()

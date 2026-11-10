# google-cloud-maintainer

Scripts e ferramentas para provisionamento e manutenção de ambiente isolado do Google Cloud Vertex AI, incluindo gerenciamento de API Keys restritas, controle granular de cotas de modelos via Cloud Quotas API e tetos de gastos via Cloud Billing Budgets API.

## 1. Pacotes Python (uv) e Autenticação (gcloud auth)

> **Atenção (Detalhe de empacotamento PyPI):** Embora o módulo Python da Cloud Quotas API seja importado como `from google.cloud import cloudquotas_v1`, o pacote oficial publicado no PyPI chama-se `google-cloud-quotas` (e não `google-cloud-cloudquotas`, que falha na resolução de dependências).

### Gerenciamento com `uv`

As dependências do projeto estão declaradas em `pyproject.toml`:
- `google-cloud-service-usage` (`google.cloud.service_usage_v1`)
- `google-cloud-api-keys` (`google.cloud.api_keys_v2`)
- `google-cloud-quotas` (`google.cloud.cloudquotas_v1`)
- `google-auth` (`google.auth` + `google.auth.transport.requests`)
- `protobuf` (`google.protobuf.field_mask_pb2`)
- `requests` (chamadas REST POST/PATCH no Cloud Billing Budgets v1)

```bash
# Sincronizar dependências no ambiente virtual uv:
uv sync
```

### Comandos `gcloud auth` e Pré-requisitos IAM

A conta autenticada deve possuir as seguintes roles:
- `roles/serviceusage.serviceUsageAdmin` (no projeto dedicado)
- `roles/serviceusage.apiKeysAdmin` (no projeto dedicado)
- `roles/cloudquotas.admin` (no projeto dedicado)
- `roles/billing.costsManager` ou `roles/billing.admin` (na Billing Account)

```bash
# 1. Autenticação interativa na CLI (se necessário)
gcloud auth login
gcloud config set project SEU_PROJECT_ID

# 2. Geração de Application Default Credentials (ADC) incluindo o escopo de Cloud Billing
gcloud auth application-default login \
  --scopes="https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/cloud-billing"

# 3. Definição obrigatória do Quota Project para Cloud Quotas API e Billing Budgets API
gcloud auth application-default set-quota-project SEU_PROJECT_ID
```

---

## 2. Decisões Arquiteturais de Produção

1. **Regras estritas de spendCap na Cloud Billing Budgets API v1:** Quando `spendCap` é definido no orçamento, a API rejeita payloads genéricos caso não obedeçam simultaneamente a:
   - Escopo exato de 1 projeto (`projects/{PROJECT_NUMBER}`) e 1 serviço elegível (`services/6F81-5844-456A`).
   - `calendarPeriod: "MONTH"` e `creditTypesTreatment: "EXCLUDE_ALL_CREDITS"`.
   - Exatamente 3 `thresholdRules` (`0.5`, `0.8`, `1.0`) com `spendBasis: "CURRENT_SPEND"`.
   - `notificationsRule: {"enableProjectLevelRecipients": True}` e `ownershipScope: "ALL_USERS"`.
2. **Descoberta dinâmica + lista estática de modelos:** Em `configure_model_quotas`, além de iterar sobre `BLOCKED_MODELS`, a função `discover_quota_models` consulta `GetQuotaInfo` da métrica `GenerateContentRequestsPerMinutePerProjectPerBaseModel` para descobrir modelos adicionais expostos na região e zerá-los automaticamente, preservando apenas `ALLOWED_MODELS`.
3. **Serviço inócuo habilitado preventivamente:** `cloudtrace.googleapis.com` é habilitado em lote por `enable_required_apis` para garantir que a troca de target da API Key durante a operação de desativação (`disable`) nunca falhe por restrição a serviço desabilitado.

---

## 3. Scripts Disponíveis

### `setup_option1.py`
Script de provisionamento inicial:
- Habilita APIs obrigatórias em lote (`enable_required_apis`).
- Cria e restringe API Key dedicada ao Vertex AI (`provision_restricted_api_key`).
- Configura cotas estritas por modelo na Cloud Quotas API (`configure_model_quotas`).
- Cria o orçamento mensal com teto de gastos (`spendCap`) via Cloud Billing Budgets API (`create_spend_cap_budget`).

### `toggle_access.py`
Script operacional para controle rápido de acesso:
- `disable`: Redireciona a API Key para um serviço inócuo e zera as cotas dos modelos permitidos.
- `enable`: Restaura a restrição da API Key para o Vertex AI, restabelece cotas RPM e reativa o estado do orçamento.
- `lift-cap`: Ajusta o estado do Spend Cap para `AWAITING_NEXT_PERIOD`, permitindo despausar as operações no ciclo atual.

---

## 4. Fluxo Rápido de Uso

```bash
# Provisionamento inicial
uv run python setup_option1.py

# Configurar a variável com o resource name retornado
export BUDGET_RESOURCE_NAME="billingAccounts/012345-6789AB-CDEF01/budgets/SEU_BUDGET_ID"

# Alternar acesso conforme necessário
uv run python toggle_access.py disable
uv run python toggle_access.py enable
uv run python toggle_access.py lift-cap
```
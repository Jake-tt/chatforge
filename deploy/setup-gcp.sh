#!/usr/bin/env bash
# One-time GCP infrastructure for Chatforge. Idempotent-ish: re-running skips what exists.
#
#   PROJECT_ID=my-proj REGION=asia-northeast3 ./deploy/setup-gcp.sh
#
# Creates: Artifact Registry repo, Cloud SQL (Postgres), Memorystore (Redis), Cloud Tasks
# queue, service accounts, and Secret Manager secrets (values are prompted, never stored here).
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-northeast3}"
SQL_INSTANCE="${SQL_INSTANCE:-chatforge-pg}"
REDIS_INSTANCE="${REDIS_INSTANCE:-chatforge-redis}"
QUEUE="${QUEUE:-chatforge-default}"
RUNTIME_SA="chatforge-api@${PROJECT_ID}.iam.gserviceaccount.com"
TASKS_SA="chatforge-tasks@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com sqladmin.googleapis.com redis.googleapis.com \
  cloudtasks.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com

gcloud artifacts repositories describe chatforge --location "$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create chatforge --repository-format docker --location "$REGION"

# --- Service accounts: the API's runtime identity, and the identity Cloud Tasks signs as.
gcloud iam service-accounts describe "$RUNTIME_SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create chatforge-api --display-name "Chatforge API runtime"
gcloud iam service-accounts describe "$TASKS_SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create chatforge-tasks --display-name "Chatforge Cloud Tasks invoker"

for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/cloudtasks.enqueuer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$RUNTIME_SA" \
    --role "$role" --condition None >/dev/null
done
# The API enqueues tasks that run *as* TASKS_SA, so it needs actAs on that account.
gcloud iam service-accounts add-iam-policy-binding "$TASKS_SA" \
  --member "serviceAccount:$RUNTIME_SA" --role roles/iam.serviceAccountUser >/dev/null

# --- Data stores
gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1 ||
  gcloud sql instances create "$SQL_INSTANCE" --database-version POSTGRES_17 \
    --region "$REGION" --tier db-f1-micro --edition enterprise
gcloud sql databases describe chatforge --instance "$SQL_INSTANCE" >/dev/null 2>&1 ||
  gcloud sql databases create chatforge --instance "$SQL_INSTANCE"
echo "Create the DB user once with:"
echo "  gcloud sql users create chatforge --instance $SQL_INSTANCE --password <generated>"

gcloud redis instances describe "$REDIS_INSTANCE" --region "$REGION" >/dev/null 2>&1 ||
  gcloud redis instances create "$REDIS_INSTANCE" --region "$REGION" --size 1 --tier basic

gcloud tasks queues describe "$QUEUE" --location "$REGION" >/dev/null 2>&1 ||
  gcloud tasks queues create "$QUEUE" --location "$REGION" \
    --max-attempts 5 --min-backoff 2s --max-backoff 60s

# --- Secrets: created empty here; add versions interactively so values never hit shell history.
for secret in DATABASE_URL JWT_SECRET ANTHROPIC_API_KEY; do
  gcloud secrets describe "$secret" >/dev/null 2>&1 ||
    gcloud secrets create "$secret" --replication-policy automatic
done
cat <<EOF

Next, add secret values (paste, then Ctrl-D):
  gcloud secrets versions add DATABASE_URL --data-file=-
      # postgresql+asyncpg://chatforge:<pw>@/chatforge?host=/cloudsql/${PROJECT_ID}:${REGION}:${SQL_INSTANCE}
  gcloud secrets versions add JWT_SECRET --data-file=-        # e.g. output of: openssl rand -hex 32
  gcloud secrets versions add ANTHROPIC_API_KEY --data-file=-
EOF

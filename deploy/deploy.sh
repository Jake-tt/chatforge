#!/usr/bin/env bash
# Build and release the API to Cloud Run.
#
#   PROJECT_ID=my-proj REGION=asia-northeast3 FRONTEND_ORIGIN=https://chatforge.web.app \
#     ./deploy/deploy.sh
#
# Order matters: the migration job runs (and must succeed) before traffic moves to the new
# revision, so new code never runs against an old schema. Migrations are written to be
# backward compatible with the previous revision while both are serving.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${FRONTEND_ORIGIN:?set FRONTEND_ORIGIN (CORS)}"
REGION="${REGION:-asia-northeast3}"
SQL_INSTANCE="${SQL_INSTANCE:-chatforge-pg}"
REDIS_INSTANCE="${REDIS_INSTANCE:-chatforge-redis}"
QUEUE="${QUEUE:-chatforge-default}"
SERVICE=chatforge-api
RUNTIME_SA="chatforge-api@${PROJECT_ID}.iam.gserviceaccount.com"
TASKS_SA="chatforge-tasks@${PROJECT_ID}.iam.gserviceaccount.com"

TAG="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/chatforge/api:${TAG}"
SQL_CONN="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
REDIS_HOST="$(gcloud redis instances describe "$REDIS_INSTANCE" --region "$REGION" \
  --format 'value(host)')"

gcloud builds submit backend --tag "$IMAGE"

# 1. Schema migration as a one-off Cloud Run Job.
gcloud run jobs deploy chatforge-migrate --image "$IMAGE" --region "$REGION" \
  --service-account "$RUNTIME_SA" --set-cloudsql-instances "$SQL_CONN" \
  --set-secrets DATABASE_URL=DATABASE_URL:latest \
  --command alembic --args upgrade,head --max-retries 0
gcloud run jobs execute chatforge-migrate --region "$REGION" --wait

# 2. API service. Direct VPC egress reaches Memorystore on its private IP.
# Cloud Run's deterministic URL, known before the first deploy. Cloud Tasks targets it and
# it is the OIDC audience the callback endpoint verifies.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')"
SERVICE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

# Payment webhooks stay off (no PAYMENT_WEBHOOK_SECRET) until a provider is connected:
# create the secret, then add PAYMENT_WEBHOOK_SECRET=PAYMENT_WEBHOOK_SECRET:latest below.
gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" \
  --service-account "$RUNTIME_SA" --allow-unauthenticated \
  --add-cloudsql-instances "$SQL_CONN" \
  --network default --subnet default --vpc-egress private-ranges-only \
  --cpu 1 --memory 512Mi --concurrency 80 --min-instances 0 --max-instances 10 \
  --timeout 300 \
  --set-secrets DATABASE_URL=DATABASE_URL:latest,JWT_SECRET=JWT_SECRET:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest \
  --set-env-vars "^@^ENV=prod@LOG_FORMAT=json@LLM_PROVIDER=anthropic@REDIS_URL=redis://${REDIS_HOST}:6379/0@CORS_ORIGINS=[\"${FRONTEND_ORIGIN}\"]@TASK_BACKEND=cloudtasks@GCP_PROJECT=${PROJECT_ID}@GCP_LOCATION=${REGION}@TASKS_QUEUE=${QUEUE}@TASKS_INVOKER_SA=${TASKS_SA}@TASKS_SERVICE_URL=${SERVICE_URL}@TRUSTED_PROXY_HOPS=1"

echo "API: $SERVICE_URL"
# Rollback: traffic moves to the new revision at once. To go back, point it at the
# previous one (list them with `gcloud run revisions list --service "$SERVICE"`):
#   gcloud run services update-traffic "$SERVICE" --region "$REGION" --to-revisions <REVISION>=100
# Schema migrations are expand-only between releases, so the previous revision still runs.
echo "Frontend: VITE_API_URL=$SERVICE_URL npm --prefix frontend run build, then upload dist/"

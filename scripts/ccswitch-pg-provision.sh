#!/usr/bin/env bash
# Provision the ccswitch Postgres role + database on dobby. Idempotent.
#   sudo -u postgres bash scripts/ccswitch-pg-provision.sh '<password>'
set -euo pipefail

PASS="${1:?usage: ccswitch-pg-provision.sh <password>}"
DB="${VIBERPIT_PG_DB:-${CCSWITCH_PG_DB:-ccswitch}}"
ROLE="${VIBERPIT_PG_ROLE:-${CCSWITCH_PG_ROLE:-ccswitch}}"

psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN PASSWORD '${PASS}';
  ELSE
    ALTER ROLE ${ROLE} PASSWORD '${PASS}';
  END IF;
END \$\$;
SQL

if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1; then
  psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DB} OWNER ${ROLE};"
fi

psql -v ON_ERROR_STOP=1 -d "${DB}" -c "GRANT ALL ON SCHEMA public TO ${ROLE};"
echo "provisioned: role=${ROLE} db=${DB}"

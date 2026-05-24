#!/usr/bin/env bash
# One-time AWS bootstrap so GitHub Actions can run iagctl on the IAG5 EC2 box via SSM.
#
# What this does:
#   1. Creates (or reuses) the GitHub OIDC provider in your AWS account.
#   2. Creates an IAM role trusted by GitHub OIDC, scoped to ONE repo + branch.
#   3. Attaches a least-privilege inline policy: ssm:SendCommand / GetCommandInvocation
#      only against the IAG5 instance, plus the AWS-RunShellScript document.
#   4. Ensures the IAG5 instance has an instance profile with AmazonSSMManagedInstanceCore.
#
# Run from a workstation with AWS creds that can manage IAM + EC2.
# Idempotent — safe to re-run.

set -euo pipefail

# ---- EDIT THESE ----
AWS_REGION="us-east-1"
GH_OWNER="michaelelrom"
GH_REPO="shared-lab-iag-assets"
GH_BRANCH="main"
IAG5_INSTANCE_ID="i-0dcf9db60fabecc0d"  # EC2 instance id of 52.204.154.11
ROLE_NAME="gha-deploy-iag5"
INSTANCE_PROFILE_NAME="iag5-ssm-profile"
INSTANCE_ROLE_NAME="iag5-ssm-role"
# --------------------

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
OIDC_PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

echo "==> AWS account ${ACCOUNT_ID} / region ${AWS_REGION}"

# 1. GitHub OIDC provider (one per account; create if missing)
if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "${OIDC_PROVIDER_ARN}" >/dev/null 2>&1; then
  echo "==> Creating GitHub OIDC provider"
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
else
  echo "==> GitHub OIDC provider already exists"
fi

# 2. Deploy role trusted by the one repo + branch
TRUST_POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "${OIDC_PROVIDER_ARN}" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:${GH_OWNER}/${GH_REPO}:ref:refs/heads/${GH_BRANCH}"
      }
    }
  }]
}
JSON
)

if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "==> Updating trust policy on ${ROLE_NAME}"
  aws iam update-assume-role-policy --role-name "${ROLE_NAME}" --policy-document "${TRUST_POLICY}"
else
  echo "==> Creating role ${ROLE_NAME}"
  aws iam create-role --role-name "${ROLE_NAME}" --assume-role-policy-document "${TRUST_POLICY}" >/dev/null
fi

# Least-privilege inline policy
PERM_POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SendIagctlCommand",
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ec2:${AWS_REGION}:${ACCOUNT_ID}:instance/${IAG5_INSTANCE_ID}",
        "arn:aws:ssm:${AWS_REGION}::document/AWS-RunShellScript"
      ]
    },
    {
      "Sid": "ReadInvocationResult",
      "Effect": "Allow",
      "Action": [
        "ssm:GetCommandInvocation",
        "ssm:ListCommandInvocations",
        "ssm:DescribeInstanceInformation"
      ],
      "Resource": "*"
    }
  ]
}
JSON
)
echo "==> Attaching inline policy gha-deploy-iag5-policy"
aws iam put-role-policy --role-name "${ROLE_NAME}" --policy-name gha-deploy-iag5-policy --policy-document "${PERM_POLICY}"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo
echo "Deploy role ARN: ${ROLE_ARN}"
echo

# 3. Instance profile for the IAG5 box (so SSM can reach it)
if ! aws iam get-role --role-name "${INSTANCE_ROLE_NAME}" >/dev/null 2>&1; then
  echo "==> Creating EC2 instance role ${INSTANCE_ROLE_NAME}"
  EC2_TRUST=$(cat <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
)
  aws iam create-role --role-name "${INSTANCE_ROLE_NAME}" --assume-role-policy-document "${EC2_TRUST}" >/dev/null
  aws iam attach-role-policy --role-name "${INSTANCE_ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
fi

PROFILE_FRESHLY_CREATED=0
if ! aws iam get-instance-profile --instance-profile-name "${INSTANCE_PROFILE_NAME}" >/dev/null 2>&1; then
  echo "==> Creating instance profile ${INSTANCE_PROFILE_NAME}"
  aws iam create-instance-profile --instance-profile-name "${INSTANCE_PROFILE_NAME}" >/dev/null
  aws iam add-role-to-instance-profile \
    --instance-profile-name "${INSTANCE_PROFILE_NAME}" \
    --role-name "${INSTANCE_ROLE_NAME}"
  PROFILE_FRESHLY_CREATED=1
fi

CURRENT_PROFILE=$(aws ec2 describe-iam-instance-profile-associations \
  --filters "Name=instance-id,Values=${IAG5_INSTANCE_ID}" \
  --query 'IamInstanceProfileAssociations[0].IamInstanceProfile.Arn' --output text 2>/dev/null || echo "None")

if [[ "${CURRENT_PROFILE}" == "None" || "${CURRENT_PROFILE}" == "null" ]]; then
  echo "==> Associating instance profile with ${IAG5_INSTANCE_ID}"
  if [[ "${PROFILE_FRESHLY_CREATED}" == "1" ]]; then
    echo "    (instance profile is fresh, waiting for IAM propagation to EC2...)"
  fi
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if aws ec2 associate-iam-instance-profile \
        --instance-id "${IAG5_INSTANCE_ID}" \
        --iam-instance-profile Name="${INSTANCE_PROFILE_NAME}" >/dev/null 2>&1; then
      echo "    associated."
      break
    fi
    [ $i -eq 10 ] && { echo "    failed after 10 retries"; exit 1; }
    sleep 6
  done
else
  echo "==> Instance already has an instance profile: ${CURRENT_PROFILE}"
  echo "    (Leaving as-is. Make sure it has AmazonSSMManagedInstanceCore attached.)"
fi

echo
echo "Done. Next steps:"
echo "  1. Confirm SSM agent on the box: sudo systemctl status amazon-ssm-agent"
echo "     (If missing on Rocky 9: sudo dnf install -y https://s3.${AWS_REGION}.amazonaws.com/amazon-ssm-${AWS_REGION}/latest/linux_amd64/amazon-ssm-agent.rpm && sudo systemctl enable --now amazon-ssm-agent)"
echo "  2. Add these to the GitHub repo:"
echo "       Secret AWS_DEPLOY_ROLE_ARN = ${ROLE_ARN}"
echo "       Variable AWS_REGION         = ${AWS_REGION}"
echo "       Variable IAG5_INSTANCE_ID   = ${IAG5_INSTANCE_ID}"
echo "       Variable IAGCTL_BIN         = /usr/local/bin/iagctl   (adjust if different)"
echo "       Variable WORK_DIR           = /home/rocky/iag-deploy   (adjust if different)"

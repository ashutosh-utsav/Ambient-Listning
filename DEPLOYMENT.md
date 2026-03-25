# Deployment

Single EC2 instance (`t4g.small`, `ap-south-1`) running web + worker + Redis
via Docker Compose. All AWS resources are managed with OpenTofu.

---

## Prerequisites

- [OpenTofu](https://opentofu.org/docs/intro/install/) >= 1.11
- AWS CLI configured with the `pdesk` profile (see below)
- An EC2 key pair already created in `ap-south-1` (for emergency SSH access)

### AWS profile setup

The `pdesk` profile targets the `indice-developer` IAM user on the psychdesk
AWS account (`894792044488`). Set it up once on any machine that will run
`tofu` commands:

`~/.aws/config`:
```ini
[pdesk]
region = ap-south-1
output = json
```

`~/.aws/credentials`:
```ini
[pdesk]
aws_access_key_id     = <get from AWS console or whoever manages the account>
aws_secret_access_key = <get from AWS console or whoever manages the account>
```

Verify it works:
```bash
aws sts get-caller-identity --profile pdesk
# should return account 894792044488, user indice-developer
```

---

## First-time setup

### 1. Bootstrap remote state

The main config stores its state in S3 with DynamoDB locking. Run this once
to create those resources. Their own state lives locally in
`infra/bootstrap/terraform.tfstate` — commit that file, it never changes.

```bash
cd infra/bootstrap
AWS_PROFILE=pdesk tofu init
AWS_PROFILE=pdesk tofu apply
```

This creates:
- S3 bucket `ambient-listning-tfstate` (versioned, encrypted)
- DynamoDB table `ambient-listning-tfstate-lock`

### 2. Init the main config

```bash
cd infra
AWS_PROFILE=pdesk tofu init
```

On first run this initialises the S3 backend. On subsequent runs (e.g. after
pulling changes) it just refreshes the provider lock.

### 3. Plan

Always plan before applying. Pass real values for the sensitive vars:

```bash
AWS_PROFILE=pdesk tofu plan \
  -var="openai_api_key=sk-..." \
  -var="key_pair_name=your-keypair-name"
```

Review the output. No surprises = safe to apply.

### 4. Apply

```bash
AWS_PROFILE=pdesk tofu apply \
  -var="openai_api_key=sk-..." \
  -var="key_pair_name=your-keypair-name"
```

After apply, note the outputs:

```
app_url        = "http://<public-ip>:8000"
ecr_web_url    = "<account>.dkr.ecr.ap-south-1.amazonaws.com/ambient-listning-web"
ecr_worker_url = "<account>.dkr.ecr.ap-south-1.amazonaws.com/ambient-listning-worker"
```

The EC2 instance will pull images and start containers automatically via
`userdata.sh.tpl`. Give it ~3 minutes after apply, then:

```bash
curl http://<public-ip>:8000/health/detailed
```

---

## Day-to-day infra changes

The loop is always: edit TF files -> plan -> review -> apply.

```bash
cd infra
AWS_PROFILE=pdesk tofu plan  -var="openai_api_key=sk-..." -var="key_pair_name=..."
AWS_PROFILE=pdesk tofu apply -var="openai_api_key=sk-..." -var="key_pair_name=..."
```

State is remote in S3 so multiple people can run this safely (DynamoDB
provides a lock that prevents concurrent applies).

---

## GitHub Actions deployment (CI/CD)

Pushes to `main` automatically build images, push to ECR, and restart
containers on the EC2 via SSM (no SSH port required).

### How the GHA IAM auth works (OIDC)

GitHub Actions assumes an AWS IAM role directly using OpenID Connect — no
long-lived access keys stored in GitHub secrets.

The trust works like this:
```
GitHub Actions runner
  -> presents a signed JWT to AWS STS
  -> AWS verifies it against github.com OIDC provider
  -> STS issues short-lived credentials for the deploy role
```

### Setting up OIDC (one-time, in the AWS console or via CLI)

**Step 1 — Create the OIDC provider** (once per AWS account):

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  --profile pdesk
```

**Step 2 — Create the deploy IAM role**

Create a role with this trust policy (replace `ORG/REPO` with your GitHub
`org/repo` slug):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:ORG/REPO:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

Attach this inline policy to the role (replace the ARNs with your account):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": [
        "arn:aws:ecr:ap-south-1:<ACCOUNT_ID>:repository/ambient-listning-web",
        "arn:aws:ecr:ap-south-1:<ACCOUNT_ID>:repository/ambient-listning-worker"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ec2:ap-south-1:<ACCOUNT_ID>:instance/*",
        "arn:aws:ssm:ap-south-1::document/AWS-RunShellScript"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "ssm:GetCommandInvocation",
      "Resource": "*"
    }
  ]
}
```

**Step 3 — Add one GitHub secret**

In the repo: Settings -> Secrets -> Actions -> New repository secret:

| Name | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/<role-name>` |

No access keys. That's it.

### The deploy workflow

`.github/workflows/deploy.yml` (to be created) does:

1. Checks out code
2. Authenticates to AWS via OIDC using `aws-actions/configure-aws-credentials`
3. Logs into ECR
4. Builds and pushes `web` image from `Dockerfile.web`
5. Builds and pushes `worker` image from `Dockerfile.worker`
6. Runs `docker compose pull && docker compose up -d` on the EC2 via
   `aws ssm send-command`

---

## Tearing everything down

```bash
cd infra
AWS_PROFILE=pdesk tofu destroy \
  -var="openai_api_key=placeholder" \
  -var="key_pair_name=placeholder"

# Then destroy the state infra itself
cd bootstrap
AWS_PROFILE=pdesk tofu destroy
```

Note: the S3 recordings bucket has `force_destroy = false` by default.
You will need to empty it manually before destroy will succeed, or set
`force_destroy = true` in `s3.tf` first.

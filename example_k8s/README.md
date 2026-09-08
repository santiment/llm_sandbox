# example_k8s — a worked example, not a deployment

These manifests show one complete way to run llm-sandbox on Kubernetes: namespace with a
PodSecurity ceiling, least-privilege access rules (RBAC), ResourceQuota + LimitRange, NetworkPolicies for
session egress, the ValidatingAdmissionPolicy that pins session pods to gVisor, and the
service Deployment. They are kept in this repo so anyone deploying the service has a
reviewed starting point.

They are **not** what runs anywhere. Registry, RuntimeClass name, node group, sizing and
secrets are placeholders (`CHANGEME`, `your-registry.example.com`). Santiment's actual
manifests live in the devops repo (`stage/k8s-apps/llm_sandbox/`) and are applied by ArgoCD
from there — changes to the real deployment go in that repo, and this directory is updated
only to keep the example current with the service's configuration surface.

`secret.yaml.example` is deliberately not a `.yaml`: it is a placeholder token and must never
be applied over a real one. See the main README, "Deploy on Kubernetes".

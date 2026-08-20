---
applyTo: "packaging/**,scripts/build_*.ps1,scripts/build_*.sh,scripts/fetch_*.ps1,scripts/generate_sbom.py,.github/workflows/release-*.yml,pyproject.toml,requirements.txt,constraints*.txt"
---

Read `AGENTS.md`, `docs/release/RELEASING.md`, `docs/security/supply-chain.md`, `THIRD_PARTY_NOTICES.md`, `SOURCE_OFFER.md` and `docs/DOCUMENTATION_POLICY.md` before changing packaging or dependencies.

A dependency, staged component, installer, artifact or release workflow change is incomplete until version/license/source/SBOM/release documentation impact has been checked. Never commit staged binaries or generated release inputs.

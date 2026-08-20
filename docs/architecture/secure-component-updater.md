# Secure component updater — design record

Status: **Accepted design; independent packaged-component updater is not implemented**

BananaFlow currently treats a full application release as the normal way packaged users receive updated bundled components. Source environments can support explicit dependency updates. This document defines the minimum security bar for any future updater that downloads/replaces executable/script components independently of a full app release.

## Why a partial updater is not acceptable

An updater is a code-execution supply-chain path. Downloading a “latest” binary/script and replacing a live component without authenticated metadata, exact integrity verification, compatibility checks, atomic activation and rollback would create a new security surface worse than the current full-release model.

## Required design properties

A future implementation must provide all of the following as one coherent design:

### Authenticated manifest

BananaFlow-controlled manifest identifies exact component version, expected size/hash, artifact URL, compatibility range and emergency-disable/superseded state. The manifest must itself be authenticated (for example a project-held signing key or another independently reviewable authenticated mechanism), not merely fetched over an arbitrary URL.

### Exact integrity and transport

- HTTPS with normal certificate validation;
- exact SHA-256 (or stronger reviewed digest) checked before activation;
- expected size / bounded download behavior;
- no `nocheckcertificate`-style bypass;
- no execution of a partially downloaded artifact.

### Compatibility

Manifest metadata declares compatible BananaFlow versions/component combinations. An incompatible update is reported rather than installed.

### App-data overlay, not live in-place mutation

Downloaded components should install into versioned per-user app-data storage rather than rewriting the installed program tree while the app is using it. Activation changes a small pointer/selection only after verification.

### Atomic install and rollback

- download to temporary state;
- verify hash/size;
- unpack/prepare safely;
- run component-specific health check;
- atomically activate only after success;
- retain at least one last-known-good version;
- roll back if health check/activation fails.

### Concurrency and shutdown

Do not swap components during active operations that use them. Do not start an install during shutdown. An interrupted download/preparation is recognized/cleaned on next launch without changing the active component.

### User control

Checking for an update and installing one are separate concepts. Automatic installation, if ever offered, is an explicit opt-in. A manual verified update path remains available.

### Safe logging

Update logs use centralized redaction and never contain credentials. Public update artifacts/manifests should not require bearer secrets to download.

### Emergency response

The authenticated control plane can disable/supersede a known-bad downloaded component without requiring the client to run it first.

## Current behavior

- Application update checks query the official BananaFlow release feed.
- Component-version checks can compare selected source/developer dependencies.
- Packaged components are refreshed by publishing a new full BananaFlow release.
- Packaged users are not offered a silent independent component replacement mechanism.

## Change rule

Any implementation of this design is security-sensitive. It requires threat-model, privacy/network, supply-chain, release, user-guide and test updates in the same PR. Do not implement a “temporary” updater that omits authentication/rollback/health-check requirements.

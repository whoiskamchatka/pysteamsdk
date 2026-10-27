# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Typed, dependency-free `pysteamsdk` package for 64-bit CPython 3.11+.
- CI on Windows, Linux and macOS: lint, format, type and test checks, package builds, and a
  check that keeps Steamworks SDK files out of the repository and distributions.
- Internal loader that finds, checks and loads the Steamworks library without initializing
  Steam, and the exceptions in `pysteamsdk.errors`.
- `pysteamsdk.init()`, `shutdown()` and `is_initialized()`: one Steam session per process,
  with typed errors that carry Steam's own failure message.

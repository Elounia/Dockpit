# Contributing to Dockpit

Thank you for your interest in contributing to **Dockpit**!

We welcome pull requests for bug fixes, performance enhancements, and documentation improvements.

---

## Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Elounia/dockpit.git
   cd dockpit
   ```

2. **Run tests:**
   Dockpit has **zero external dependencies** and uses Python 3's built-in `unittest` runner:
   ```bash
   python3 -m unittest discover -s tests -v
   ```

3. **Run locally:**
   ```bash
   ./bin/dockpit
   # Or: python3 -m dockpit
   ```
   Open your browser to `http://localhost:8989`.

---

## Coding Standards

- **Standard Library Only:** The core engine of Dockpit must remain zero-dependency. Do not add heavy web frameworks like Flask, FastAPI, or requests to core requirements.
- **Security First:** Never expose secrets or environment variables in inspection outputs. Always validate and sanitize container names and log tails.
- **Simplicity:** Keep frontend UI responsive, clean, and fast. Avoid adding heavy asset bundles.
- **Test Coverage:** All new features or bug fixes must include unit tests in `tests/test_dockpit.py`.

---

## License

By contributing to Dockpit, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).


from pathlib import Path


def test_docker_runtime_installs_excel_dependency():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert '"openpyxl>=3.1"' in dockerfile

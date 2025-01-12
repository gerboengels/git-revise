# pylint: skip-file

import pytest
import shutil
import os
import sys
import textwrap
import subprocess
import traceback
from pathlib import Path
from gitrevise.odb import Repository
from contextlib import contextmanager
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue, Empty


RESOURCES = Path(__file__).parent / "resources"


@pytest.fixture
def bash(repo):
    def run_bash(command, check=True, cwd=repo.workdir):
        subprocess.run(["bash", "-ec", textwrap.dedent(command)], check=check, cwd=cwd)

    return run_bash


def _docopytree(source, dest, renamer=lambda x: x):
    for dirpath, _, filenames in os.walk(source):
        srcdir = Path(dirpath)
        reldir = srcdir.relative_to(source)

        for name in filenames:
            srcf = srcdir / name
            destf = dest / renamer(reldir / name)
            destf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcf, destf)


class TestRepo(Repository):
    """repository object with extra helper methods for writing tests"""

    def load_template(self, name):
        def renamer(path):
            # If a segment named _git is present, replace it with .git.
            return Path(*[".git" if p == "_git" else p for p in path.parts])

        _docopytree(RESOURCES / name, self.workdir, renamer=renamer)


@pytest.fixture
def repo(tmp_path_factory, monkeypatch):
    # Create a working directory, and start the repository in it.
    # We also change into a different temporary directory to make sure the code
    # doesn't require pwd to be the workdir.
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))

    workdir = tmp_path_factory.mktemp("repo")
    subprocess.run(["git", "init", "-q"], check=True, cwd=workdir)
    return TestRepo(workdir)


@pytest.fixture
def main(repo):
    # Run the main entry point for git-revise in a subprocess.
    def main(args, **kwargs):
        kwargs.setdefault("cwd", repo.workdir)
        subprocess.run([sys.executable, "-m", "gitrevise", *args], **kwargs)

    return main


@pytest.fixture
def fake_editor(tmp_path_factory, monkeypatch):
    @contextmanager
    def fake_editor(handler):
        tmpdir = tmp_path_factory.mktemp("editor")

        # Write out the script
        script = textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            from pathlib import Path
            from urllib.request import urlopen

            path = Path(sys.argv[1])
            resp = urlopen("http://127.0.0.1:8190/", data=path.read_bytes())
            length = int(resp.headers.get("content-length"))
            path.write_bytes(resp.read(length))
            """
        )
        script_path = tmpdir / "editor"
        script_path.write_bytes(script.encode())
        script_path.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(script_path))

        inq = Queue()
        outq = Queue()
        excq = Queue()

        def wrapper():
            try:
                handler(inq, outq)
            except Exception:
                traceback.print_exc()
                excq.put(sys.exc_info())

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length"))
                inq.put(self.rfile.read(length))
                new = outq.get(timeout=1)

                self.send_response(200)
                self.send_header("content-length", len(new))
                self.end_headers()
                self.wfile.write(new)

        # Start the HTTP manager
        server = HTTPServer(("127.0.0.1", 8190), Handler)
        try:
            Thread(target=wrapper, daemon=True).start()
            Thread(target=server.serve_forever, daemon=True).start()

            yield

            # Re-raise any queued exceptions
            try:
                raise excq.get_nowait()
            except Empty:
                pass
        finally:
            server.shutdown()
            server.server_close()

    return fake_editor

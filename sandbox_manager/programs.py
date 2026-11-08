"""Programs installed on this PC, shared read-only into a running Sandbox (배주한 요청 2026-10-27).

When the Agent needs a compiler or interpreter that the clean Guest does not have, the Host can share
the program's own install folder into the running Sandbox with `wsb share` (no restart). Found on
2026-10-27: a share appears in the Guest within a second, is read-only unless -w is given, and the
Host folder is unchanged afterwards (6,146 files, 0 changed).

Rules:
- only programs in PROGRAMS: a compiler or interpreter that runs from its own folder. Programs that need
  an installer, a service or a licence (Office, Adobe, Visual Studio) cannot work this way and are not listed
- always read-only: WsbCli.share() has no way to ask for write access
- the shared folder is the program's install folder only, never the user's profile or one of its parents;
  inside the profile only AppData\\Local\\Programs\\... is accepted (where Python installs per user)
- no reparse point (junction, symlink) anywhere inside it: it could lead to other Host folders.
  Hard-linked files are counted, not refused: Git for Windows hard-links its own executables
- the Guest writes caches and packages to its own disk (env below), never into the shared folder

Not installed on this PC: the Agent tells the user to install it (official installer) and try again.
Nothing is fetched for it (배주한 2026-11-03/11-07: the portable zip auto-fetch of 10-31 was removed:
fetching and updating languages is not this project's job, and pinned URLs and digests break).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .errors import INVALID_ARGUMENT, SandboxManagerError

GUEST_TOOLS = r"C:\Tools"
SCAN_MAX_FILES = 60_000                  # MSYS2 ucrt64 has ~22k files; a larger folder is not a single program


@dataclass(frozen=True)
class Program:
    exe: str                             # what `which` looks for
    marker: str                          # the exe relative to the install folder: how the folder is recognised
    path: tuple[str, ...]                # folders (relative to the install folder) for PATH in the Guest
    env: tuple[tuple[str, str], ...]     # Guest environment: caches go to the Guest's own TEMP
    known: tuple[str, ...] = ()          # usual places when it is not on PATH
    # MSYS2 gcc finds its headers at <bin>\..\..\ucrt64 (-isysroot): the folder must keep its own name
    keep_name: bool = False


PROGRAMS = {
    "python": Program("python.exe", "python.exe", ("", "Scripts"),
                      (("PYTHONPYCACHEPREFIX", r"%TEMP%\pycache"), ("PIP_CACHE_DIR", r"%TEMP%\pip-cache"))),
    "git": Program("git.exe", r"cmd\git.exe", ("cmd",), (), (r"C:\Program Files\Git\cmd\git.exe",)),
    "gcc": Program("gcc.exe", r"bin\gcc.exe", ("bin",), (),
                   (r"C:\msys64\ucrt64\bin\gcc.exe", r"C:\msys64\mingw64\bin\gcc.exe"), keep_name=True),
    "node": Program("node.exe", "node.exe", ("",), (("npm_config_cache", r"%TEMP%\npm-cache"),),
                    (r"C:\Program Files\nodejs\node.exe",)),
    "go": Program("go.exe", r"bin\go.exe", ("bin",), (("GOCACHE", r"%TEMP%\go-cache"), ("GOPATH", r"%TEMP%\gopath")),
                  (r"C:\Program Files\Go\bin\go.exe",)),
    "java": Program("java.exe", r"bin\java.exe", ("bin",), ()),     # a JDK: `java File.java` runs a source file
    # Zig (also `zig cc` / `zig c++`: clang + MinGW libc, for C/C++ on a PC with Zig but without gcc)
    "zig": Program("zig.exe", "zig.exe", ("",),
                   (("ZIG_GLOBAL_CACHE_DIR", r"%TEMP%\zig-cache"), ("ZIG_LOCAL_CACHE_DIR", r"%TEMP%\zig-local"))),
    # C#: the .NET SDK (`dotnet run app.cs` runs a single file, `#:package` pulls NuGet packages)
    "dotnet": Program("dotnet.exe", "dotnet.exe", ("",),
                      (("DOTNET_CLI_HOME", r"%TEMP%\dotnet-home"), ("NUGET_PACKAGES", r"%TEMP%\nuget"),
                       ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"), ("DOTNET_NOLOGO", "1"),
                       ("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1")),
                      (r"C:\Program Files\dotnet\dotnet.exe",)),
    # Ruby (RubyInstaller, C:\Ruby40-x64): gems go to the Guest's TEMP; gems with C code need its MSYS2 devkit
    "ruby": Program("ruby.exe", r"bin\ruby.exe", ("bin",), (("GEM_HOME", r"%TEMP%\gems"),),
                    (r"C:\Ruby40-x64\bin\ruby.exe",)),
    # PHP (windows.php.net zip, usually C:\php): no php.ini, so extensions are switched on with -d (Skill);
    # Composer (composer.phar) is fetched inside the Sandbox
    "php": Program("php.exe", "php.exe", ("",),
                   (("COMPOSER_HOME", r"%TEMP%\composer"), ("COMPOSER_CACHE_DIR", r"%TEMP%\composer-cache")),
                   (r"C:\php\php.exe",)),
}


# A shell for .sh files inside a Git folder: Git for Windows' bash (or ash in a MinGit busybox).
SHELLS = (r"bin\bash.exe", r"ucrt64\bin\ash.exe", r"mingw64\bin\ash.exe")


@dataclass(frozen=True)
class FoundProgram:
    name: str
    root: Path                           # Host install folder
    exe: str                             # exe path relative to root
    hard_links: int = 0
    source: str = "installed"            # always installed on this PC (kept in the answer for the Agent)

    @property
    def guest_root(self) -> str:
        if PROGRAMS[self.name].keep_name:
            return f"{GUEST_TOOLS}\\{self.name}\\{self.root.name}"
        return f"{GUEST_TOOLS}\\{self.name}"

    def guest_path(self) -> list[str]:
        return [str(PureWindowsPath(self.guest_root, p)) if p else self.guest_root for p in PROGRAMS[self.name].path]

    def for_agent(self) -> dict:
        """What the Agent needs in the Guest: where it is and how to set up a command prompt."""
        answer = {"name": self.name, "guest_root": self.guest_root, "exe": f"{self.guest_root}\\{self.exe}",
                  "path": self.guest_path(), "env": dict(PROGRAMS[self.name].env), "cmd_setup": cmd_setup([self]),
                  "read_only": True, "source": self.source}
        shell = next((s for s in SHELLS if (self.root / s).is_file()), None)
        if self.name == "git" and shell:
            answer["shell"] = f"{self.guest_root}\\{shell}"     # run .sh files with this
        return answer


def cmd_setup(found: list[FoundProgram]) -> str:
    """One cmd line for all of them. Separate `set "PATH=x;%PATH%"` joined with & would each expand
    %PATH% before any of them runs, so only the last would count."""
    path = [d for f in found for d in f.guest_path()]
    env = {k: v for f in found for k, v in PROGRAMS[f.name].env}
    return " & ".join([f'set "PATH={";".join(path)};%PATH%"'] + [f'set "{k}={v}"' for k, v in env.items()])


def _python_base(exe: Path) -> Path:
    """A venv's python.exe only works next to its base install: share the base (pyvenv.cfg `home`)."""
    for cfg in (exe.parent / "pyvenv.cfg", exe.parent.parent / "pyvenv.cfg"):
        if cfg.is_file():
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.partition("=")
                if key.strip().lower() == "home" and value.strip():
                    return Path(value.strip()) / "python.exe"
    return exe


def find_program(name: str, *, which=shutil.which, check=None) -> FoundProgram:
    """This PC's installed copy that `check` (check_program_root) accepts.

    Every candidate is tried in turn (PATH first, then the usual install places). Found 2026-11-03: the
    Host started by Codex has Codex's own node.exe (~/.cache/codex-runtimes/...) first on PATH, inside
    the profile and refused; an installed Node in Program Files is used then. None at all: the user is
    told to install it."""
    spec = PROGRAMS.get(name)
    if spec is None:
        raise SandboxManagerError(INVALID_ARGUMENT, f"{name!r} cannot be shared; known: {sorted(PROGRAMS)}")
    check = check or (lambda found: found)
    refused = None
    for c in [which(spec.exe.removesuffix(".exe"))] + list(spec.known):
        found = _candidate(name, c)
        if found is None:
            continue
        try:
            return check(found)
        except SandboxManagerError as e:
            refused = refused or e
    if refused is not None:
        raise refused
    raise SandboxManagerError(INVALID_ARGUMENT, f"{name} is not installed on this PC; ask the user to install it "
                                                "(its official installer) and try again")


def _candidate(name: str, c: str | None) -> FoundProgram | None:
    if not c:
        return None
    spec = PROGRAMS[name]
    exe = Path(c)
    if "windowsapps" in (p.lower() for p in exe.parts):
        return None                                      # Microsoft Store alias, not a real install
    if name == "python":
        exe = _python_base(exe)
    if not exe.is_file():
        return None
    exe = exe.resolve()
    # git.exe also lives in Git/mingw64/bin: climb to the folder that has the marker
    root = next((d for d in exe.parents if (d / spec.marker).is_file()), None)
    if root is None or root.parent == root:
        return None
    return FoundProgram(name, root, spec.marker)


def find_installed(name: str, *, which=shutil.which) -> FoundProgram:
    """The first installed copy, unchecked."""
    return find_program(name, which=which)


def check_program_root(found: FoundProgram, *, home: Path | None = None) -> FoundProgram:
    """Refuse a folder that would show the Guest more than one program; returns it with hard_links counted."""
    root = found.root
    home = (home or Path(os.path.expanduser("~"))).resolve()
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise SandboxManagerError(INVALID_ARGUMENT, f"{root} is not a plain folder")
    if root.parent == root:
        raise SandboxManagerError(INVALID_ARGUMENT, f"{root} is a drive root")
    if root == home or root in home.parents:
        raise SandboxManagerError(INVALID_ARGUMENT, f"{root} contains the user's profile")
    if home in root.parents:
        # compare resolved paths: a packaged app may see AppData\Local redirected (MSIX virtualisation)
        # (both forms: a folder may exist partly in the real and partly in the redirected place)
        programs = home / "AppData" / "Local" / "Programs"   # per-user installs (Python, Node, VS Code...)
        allowed = {programs, programs.resolve()}
        if not any(a in r.parents for a in allowed for r in (root, root.resolve())):
            raise SandboxManagerError(INVALID_ARGUMENT, f"{root} is inside the user's profile")
    files = hard_links = 0
    stack = [root]
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in it:
                if e.is_symlink() or e.is_junction():
                    raise SandboxManagerError(INVALID_ARGUMENT, f"link inside {root}: {e.path}")
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                    continue
                files += 1
                if files > SCAN_MAX_FILES:
                    raise SandboxManagerError(INVALID_ARGUMENT, f"{root} has more than {SCAN_MAX_FILES} files")
                if os.lstat(e.path).st_nlink > 1:    # DirEntry.stat() leaves st_nlink 0 on Windows
                    hard_links += 1
    return FoundProgram(found.name, root, found.exe, hard_links, found.source)

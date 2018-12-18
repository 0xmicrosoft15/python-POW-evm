from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import venv


def create_venv(parent_path):
    venv_path = parent_path / 'package-smoke-test'
    venv.create(venv_path, with_pip=True)
    subprocess.run([venv_path / 'bin' / 'pip', 'install', '-U', 'pip', 'setuptools'], check=True)
    return venv_path


def find_wheel(project_path):
    wheels = list(project_path.glob('dist/*.whl'))

    if len(wheels) != 1:
        raise Exception(
            f"Expected one wheel. Instead found: {wheels} in project {project_path.absolute()}"
        )

    return wheels[0]


def install_wheel(venv_path, wheel_path, extras=tuple()):
    if extras:
        extra_suffix = f"[{','.join(extras)}]"
    else:
        extra_suffix = ""

    subprocess.run(
        [
            venv_path / 'bin' / 'pip',
            'install',
            f"{wheel_path}{extra_suffix}"
        ],
        check=True,
    )


def test_install_local_wheel():
    temporary_dir = TemporaryDirectory()
    venv_path = create_venv(Path(temporary_dir.name))
    wheel_path = find_wheel(Path('.'))
    install_wheel(venv_path, wheel_path, extras=['p2p', 'trinity'])
    print("Installed", wheel_path.absolute(), "to", venv_path)
    print(f"Activate with `source {venv_path}/bin/activate`")
    input("Press enter when the test has completed. The directory will be deleted.")
    temporary_dir.cleanup()


if __name__ == '__main__':
    test_install_local_wheel()

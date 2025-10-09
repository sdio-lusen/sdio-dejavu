from setuptools import find_packages, setup


def parse_requirements(requirements):
    # load from requirements.txt
    with open(requirements) as f:
        lines = [l for l in f]
        # remove spaces
        stripped = list(map((lambda x: x.strip()), lines))
        # remove comments
        nocomments = list(filter((lambda x: not x.startswith('#')), stripped))
        # remove empty lines
        reqs = list(filter((lambda x: x), nocomments))
        return reqs


PACKAGE_NAME = "sdio-dejavu"
PACKAGE_VERSION = "0.1.3+sdio"
SUMMARY = "SDIO-Dejavu: Customized audio fingerprinting for CM detection"
DESCRIPTION = """
Customized version of Dejavu for TV Commercial (CM) detection.

Based on the original project by Will Drevo:
http://willdrevo.com/fingerprinting-and-audio-recognition-with-python/
"""
REQUIREMENTS = parse_requirements("requirements.txt")

setup(
    name=PACKAGE_NAME,
    version=PACKAGE_VERSION,
    description=SUMMARY,
    long_description=DESCRIPTION,
    author='Lusen Kong',
    author_email='lusen@sdio.co.jp',
    maintainer="Lusen Kong",
    maintainer_email="lusen@sdio.co.jp",
    url='https://github.com/sdio-lusen/sdio-dejavu',
    license='MIT License',
    include_package_data=True,
    packages=find_packages(include=["sdio_dejavu", "sdio_dejavu.*"]),
    platforms=["Unix", "MacOS", "Windows"],
    install_requires=REQUIREMENTS,
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
    keywords="python, audio, fingerprinting, music, dejavu, sdio, cm detection",
)

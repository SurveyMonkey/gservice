#!/usr/bin/env python
from setuptools import Command
from setuptools import find_packages
from setuptools import setup
import os

def command(fn):
    def wrapped():
        class cmdclass(Command):
            def initialize_options(self): pass
            def finalize_options(self): pass
            user_options = []
            description = fn.__doc__
            def run(self): fn()
        return cmdclass
    return wrapped

@command    
def test():
    """run tests with nose"""
    os.execlp("nosetests", "nosetests")

@command
def build_pages():
    """rebuild the website"""
    os.execlp("bash", "bash", "-c", """branch=$(git status | grep 'On branch' | cut -f 4 -d ' ')
        git checkout gh-pages && 
        git commit --allow-empty -m 'trigger pages rebuild' && 
        git push origin gh-pages && 
        git checkout $branch""")

@command
def coverage():
    """run test coverage report with nose"""
    os.execlp("nosetests", "nosetests", "--with-coverage", "--cover-package=gservice")

requires = ['gevent>=0.13.3',
            'setproctitle',
            'nose',
            'python-daemon']

setup(
    name='gservice',
    version='0.3.0',
    author='Jeff Lindsay',
    long_description=open('README.md').read(),
    author_email='jeff.lindsay@twilio.com',
    description='Lightweight service framework',
    packages=find_packages(),
    install_requires=requires,
    data_files=[],
    entry_points="""
    [console_scripts]
    gservice=gservice.runner:main
    """,
    cmdclass={
        'test': test(),
        'coverage': coverage(),
        'build_pages': build_pages()
        }
)

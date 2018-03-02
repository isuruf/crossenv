import venv
import os
import sysconfig
import glob
import sys
import shutil
from textwrap import dedent
import subprocess
import logging
import importlib
import types
from configparser import ConfigParser

from .utils import F
from . import utils

logger = logging.getLogger(__name__)

class CrossEnvBuilder(venv.EnvBuilder):
    """
    A class to build a cross-compiling virtual environment useful for
    cross compiling wheels or developing firmware images.

    Here the `host` is the device on which the final code will run, such
    as an embedded system of some sort. `build` is the machine doing the
    compiling, usually a desktop or server. Usually the `host` Python
    executables won't run on the `build` machine.

    When we refer to `build-python`, we mean the current interpreter.  (It is
    *always* the current interpreter.) When we refer to `host-pytohn`, we mean
    the interpreter that will run on the host. When we refer to `cross-python`,
    we mean an interpreter that runs on `build` but reports system information
    as if it were running on `host`. In other words, `cross-python` does the
    cross compiling, and is what this class will create for us.

    You must have the toolchain used to compile the host Python binary
    available when using this virtual environment. The virtual environment
    will pick the correct compiler based on info recorded when the host
    Python binary was compiled.

    :param host_python:     The path to the host Python binary. This may be in
                            a build directory (i.e., after `make`), or in an
                            install directory (after `make install`).  It
                            *must* be the exact same version as build-python.

    :param extra_env_vars:  When cross-python starts, this is an iterable of
                            (name, op, value) tuples. op may be one of '=' to
                            indicate that the variable will be set
                            unconditionally, or '?=' to indicate that the
                            variable will be set only if not already set by the
                            environment.

    :param build_system_site_packages:
                            Whether or not build-python's virtual environment
                            will have access to the system site packages.
                            cross-python never has access, for obvious reasons.

    :param clear:           Whether to delete the contents of the environment
                            directories if they already exist, before
                            environment creation. May be a false value, or one
                            of 'default', 'cross', 'build', or 'both'.
                            'default' means to clear cross only when
                            cross_prefix is None.
    
    :param cross_prefix:    Explicitly set the location of the cross-python
                            virtual environment.

    :param with_cross_pip:  If True, ensure pip is installed in the
                            cross-python virtual environment.

    :param with_build_pip:  If True, ensure pip is installed in the
                            build-python virtual environment.
    """
    def __init__(self, *,
            host_python,
            extra_env_vars=(),
            build_system_site_packages=False,
            clear=False,
            cross_prefix=None,
            with_cross_pip=False,
            with_build_pip=False):
        self.find_host_python(host_python)
        self.find_compiler_info()
        self.build_system_site_packages = build_system_site_packages
        self.extra_env_vars = extra_env_vars
        self.clear_build = clear in ('default', 'build', 'both')
        self.with_cross_pip = with_cross_pip
        self.with_build_pip = with_build_pip
        if cross_prefix:
            self.cross_prefix = os.path.abspath(cross_prefix)
            self.clear_cross = clear in ('cross', 'both')
        else:
            self.cross_prefix = None
            self.clear_cross = clear in ('default', 'cross', 'both')

        super().__init__(
                system_site_packages=False,
                clear=False,
                symlinks=True,
                upgrade=False,
                with_pip=False)

    def find_host_python(self, host):
        """
        Find Python paths and other info based on a path.

        :param host:    Path to the host Python executable.
        """

        build_version = sysconfig.get_config_var('VERSION')
        host = os.path.abspath(host)
        if not os.path.exists(host):
            raise FileNotFoundError("%s does not exist" % host)
        elif not os.path.isfile(host):
            raise ValueError("Expected a path to a Python executable. "
                             "Got %s" % host)
        else:
            self.host_project_base = os.path.dirname(host)

        if sysconfig._is_python_source_dir(self.host_project_base):
            self.host_makefile = os.path.join(self.host_project_base, 'Makefile')
            pybuilddir = os.path.join(self.host_project_base, 'pybuilddir.txt')
            try:
                with open(pybuilddir, 'r') as fp:
                    build_dir = fp.read().strip()
            except IOError:
                raise IOError(
                    "Cannot read %s: Build the host Python first " % s) from None

            self.host_home = self.host_project_base
            sysconfigdata = glob.glob(
                os.path.join(self.host_project_base,
                             build_dir,
                             '_sysconfigdata*.py'))
        else:
            # Assume host_project_base == {prefix}/bin and that this Python
            # mirrors the host Python's install paths.
            self.host_home = os.path.dirname(self.host_project_base)
            python_ver = 'python' + sysconfig.get_config_var('py_version_short')
            libdir = os.path.join(self.host_home, 'lib', python_ver)
            sysconfigdata = glob.glob(os.path.join(libdir, '_sysconfigdata*.py'))
            if not sysconfigdata:
                # Ubuntu puts it in a subdir plat-...
                sysconfigdata = glob.glob(
                        os.path.join(libdir, '*', '_sysconfigdata*.py'))

                if not sysconfigdata:
                    # Try to make sense of the error. Probably a version error.
                    anylib = os.path.join(self.host_home, 'lib', 'python*')
                    glob1 = os.path.join(anylib, '_sysconfigdata*.py')
                    glob2 = os.path.join(anylib, '*', '_sysconfigdata*.py')
                    found = glob.glob(glob1)
                    found.extend(glob.glob(glob2))

                    if found:
                        found = os.path.basename(os.path.dirname(found[0]))
                        host_version = found[6:]
                        raise ValueError(
                                "Version mismatch: host=%s, build=%s" % (
                                    host_version, build_version))
                    # Let it error out later with the default message

            makefile = glob.glob(os.path.join(libdir, '*', 'Makefile'))
            if not makefile:
                self.host_makefile = '' # fail later
            else:
                self.host_makefile = makefile[0]

        if not sysconfigdata:
            raise FileNotFoundError("No _sysconfigdata*.py found in host lib")
        elif len(sysconfigdata) > 1:
            raise ValueError("Malformed Python installation.")

        # We need paths to sysconfig data, and we need to import it to ask
        # a few questions.
        self.host_sysconfigdata_file = sysconfigdata[0]
        name = os.path.basename(sysconfigdata[0])
        self.host_sysconfigdata_name, _ = os.path.splitext(name)
        spec = importlib.util.spec_from_file_location(
                self.host_sysconfigdata_name,
                self.host_sysconfigdata_file)
        syscfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(syscfg)
        self.host_sysconfigdata = syscfg

        self.host_cc = syscfg.build_time_vars['CC']
        self.host_version = syscfg.build_time_vars['VERSION']

        # Ask the makefile a few questions too
        if not os.path.exists(self.host_makefile):
            raise FileNotFoundError("Cannot find Makefile")

        self.host_platform = sys.platform # Default: not actually cross compiling
        with open(self.host_makefile, 'r') as fp:
            for line in fp:
                line = line.strip()
                if line.startswith('_PYTHON_HOST_PLATFORM='):
                    self.host_platform = line.split('=',1)[-1]
                    break

        # Sanity checks
        if self.host_version != build_version:
            raise ValueError("Version mismatch: host=%s, build=%s" % (
                self.host_version, build_version))

    def find_compiler_info(self):
        """
        Query the compiler for extra info useful for cross-compiling,
        and also check that it exists.
        """

        def run_compiler(arg):
            cmdline = [self.host_cc, arg]
            try:
                return subprocess.check_output(cmdline, universal_newlines=True)
            except CalledProcessError:
                return None

        self.host_sysroot = None

        if run_compiler('--version') is None:
            # I guess we could continue...but why?
            raise RuntimeError(
                "Cannot run cross-compiler! Extension modules won't build!")
            return

        # TODO: Clang doesn't have this option
        self.host_sysroot = run_compiler('-print-sysroot').strip()

    def create(self, env_dir):
        """
        Create a cross virtual environment in a directory

        :param env_dir: The target directory to create an environment in.
        """

        env_dir = os.path.abspath(env_dir)
        context = self.ensure_directories(env_dir)
        self.create_configuration(context)
        self.make_build_python(context)
        self.make_cross_python(context)
        self.post_setup(context)

    def ensure_directories(self, env_dir):
        """
        Create the directories for the environment.

        Returns a context object which holds paths in the environment,
        for use by subsequent logic.
        """

        # Directory structure:
        #
        # ENV_DIR/
        #   cross/      cross-python venv
        #   build/      build-python venv
        #   lib/        libs for setting up cross-python
        #   bin/        holds activate scripts.

        if os.path.exists(env_dir) and (self.clear_cross or self.clear_build):
            subdirs = os.listdir(env_dir)
            for sub in subdirs:
                if sub in ('cross', 'build'):
                    continue
                utils.remove_path(os.path.join(env_dir, sub))

        context = super().ensure_directories(env_dir)
        context.lib_path = os.path.join(env_dir, 'lib')
        utils.mkdir_if_needed(context.lib_path)
        return context

    def create_configuration(self, context):
        """
        Create configuration files. We don't have a pyvenv.cfg file in the
        base directory, but we do have a uname crossenv.cfg file.
        """

        # Do our best to guess defaults
        config = ConfigParser()
        sysname, machine = self.host_platform.split('-')
        config['uname'] = {
            'sysname' : sysname.title(),
            'nodename' : 'build',
            'release' : '',
            'version' : '',
            'machine' : machine,
        }

        context.crossenv_cfg = os.path.join(context.env_dir, 'crossenv.cfg')
        with utils.overwrite_file(context.crossenv_cfg) as fp:
            config.write(fp)

    def make_build_python(self, context):
        """
        Assemble the build-python virtual environment
        """

        context.build_env_dir = os.path.join(context.env_dir, 'build')
        logger.info("Creating build-python environment")
        env = venv.EnvBuilder(
                system_site_packages=self.build_system_site_packages,
                clear=self.clear_build,
                with_pip=self.with_build_pip)
        env.create(context.build_env_dir)
        context.build_bin_path = os.path.join(context.build_env_dir, 'bin')
        context.build_env_exe = os.path.join(
                context.build_bin_path, context.python_exe)

        # What is build-python's sys.path?
        out = subprocess.check_output(
                [context.build_env_exe,
                    '-c',
                    r"import sys; print('\n'.join(sys.path))"],
                universal_newlines=True).splitlines()
        context.build_sys_path = []
        for line in out:
            line = line.strip()
            if line:
                context.build_sys_path.append(line)

    def make_cross_python(self, context):
        """
        Assemble the cross-python virtual environment
        """

        logger.info("Creating cross-python environment")
        if self.cross_prefix:
            context.cross_env_dir = self.cross_prefix
        else:
            context.cross_env_dir = os.path.join(context.env_dir, 'cross')
        clear_cross = self.clear in ('default', 'cross-only', 'both')
        env = venv.EnvBuilder(
                system_site_packages=False,
                clear=self.clear_cross,
                symlinks=True,
                upgrade=False,
                with_pip=False)
        env.create(context.cross_env_dir)
        context.cross_bin_path = os.path.join(context.cross_env_dir, 'bin')
        context.cross_env_exe = os.path.join(
                context.cross_bin_path, context.python_exe)
        context.cross_cfg_path = os.path.join(context.cross_env_dir, 'pyvenv.cfg')
        context.cross_activate = os.path.join(context.cross_bin_path, 'activate')

        # Remove binaries. We'll run from elsewhere
        for exe in os.listdir(context.cross_bin_path):
            if not exe.startswith('activate'):
                utils.remove_path(os.path.join(context.cross_bin_path, exe))

        # Alter pyvenv.cfg
        with utils.overwrite_file(context.cross_cfg_path) as out:
            with open(context.cross_cfg_path) as inp:
                for line in inp:
                    if line.split()[0:2] == ['home', '=']:
                        line = 'home = %s\n' % self.host_project_base
                    out.write(line)

        # make a script that sets the environment variables and calls Python.
        # Don't do this in bin/activate, because it's a pain to set/unset
        # properly (and for csh, fish as well).
        
        # Note that env_exe hasn't actually been created yet.

        sysconfig_name = os.path.basename(self.host_sysconfigdata_file)
        sysconfig_name, _ = os.path.splitext(sysconfig_name)

        # If this venv is generated from a cross-python still in its
        # build directory, rather than installed, then our modifications
        # prevent build-python from finding its pure-Python libs, which
        # will cause a crash on startup. Add them back to PYTHONPATH.
        # Also: 'stdlib' might not be accurate if build-python is in a build
        # directory.
        stdlib = os.path.abspath(os.path.dirname(os.__file__))

        with open(context.cross_env_exe, 'w') as fp:
            fp.write(dedent(F('''\
                #!/bin/sh
                _base=${0##*/}
                export PYTHON_CROSSENV=1
                export _PYTHON_PROJECT_BASE="%(self.host_project_base)s"
                export _PYTHON_HOST_PLATFORM="%(self.host_platform)s"
                export _PYTHON_SYSCONFIGDATA_NAME="%(sysconfig_name)s"
                export PYTHONHOME="%(self.host_home)s"
                export PYTHONPATH="%(context.lib_path)s:%(stdlib)s${PYTHONPATH:+:$PYTHONPATH}"
                ''', locals())))

            # Add sysroot to various environment variables. This doesn't help
            # compiling, but some packages try to do manual checks for existence
            # of headers and libraries. This will help them find things.
            if self.host_sysroot:
                libs = os.path.join(self.host_sysroot, 'usr', 'lib*')
                libs = glob.glob(libs)
                if not libs:
                    logger.warning("No libs in sysroot. Does it exist?")
                else:
                    libs = os.pathsep.join(libs)
                    fp.write(F('export LIBRARY_PATH=%(libs)s\n', locals()))

                inc = os.path.join(self.host_sysroot, 'usr', 'include')
                if not os.path.isdir(inc):
                    logger.warning("No include/ in sysroot. Does it exist?")
                else:
                    fp.write(F('export CPATH=%(inc)s\n', locals()))

            for name, assign, val in self.extra_env_vars:
                if assign == '=':
                    fp.write(F('export %(name)s=%(val)s\n', locals()))
                elif assign == '?=':
                    fp.write(F('[ -z "${%(name)s}" ] && export %(name)s=%(val)s\n',
                        locals()))
                else:
                    assert False, "Bad assignment value %r" % assign

            # We want to alter argv[0] so that sys.executable will be correct.
            # We can't do this in a POSIX-compliant way, so we'll break
            # into Python
            fp.write(dedent(F('''\
                exec %(context.build_env_exe)s -c '
                import sys
                import os
                os.execv("%(context.build_env_exe)s", sys.argv[1:])
                ' "%(context.cross_bin_path)s/$_base" "$@"
                ''', locals())))
        os.chmod(context.cross_env_exe, 0o755)
        for exe in ('python', 'python3'):
            exe = os.path.join(context.cross_bin_path, exe)
            if not os.path.exists(exe):
                utils.symlink(context.python_exe, exe)

        # Install patches to environment
        utils.install_script('site.py', context.lib_path, locals())
        shutil.copy(self.host_sysconfigdata_file, context.lib_path)
       
        # host-python is ready.
        if self.with_cross_pip:
            logger.info("Installing cross-pip")
            subprocess.check_call([context.cross_env_exe, '-m', 'ensurepip',
                '--default-pip', '--upgrade'])

    def post_setup(self, context):
        """
        Extra processing. Put scripts/binaries in the right place.
        """

        # Add cross-python alias to the path. This is just for
        # convenience and clarity.
        for exe in os.listdir(context.cross_bin_path):
            target = os.path.join(context.cross_bin_path, exe)
            if not os.path.isfile(target) or not os.access(target, os.X_OK):
                continue
            dest = os.path.join(context.bin_path, 'cross-' + exe)
            utils.symlink(target, dest)

        # Add build-python and build-pip to the path.
        for exe in os.listdir(context.build_bin_path):
            target = os.path.join(context.build_bin_path, exe)
            if not os.path.isfile(target) or not os.access(target, os.X_OK):
                continue
            dest = os.path.join(context.bin_path, 'build-' + exe)
            utils.symlink(target, dest)

        logger.info("Finishing up...")
        activate = os.path.join(context.bin_path, 'activate')
        with open(activate, 'w') as fp:
            fp.write(dedent(F('''\
                . %(context.cross_activate)s
                export PATH=%(context.bin_path)s:$PATH
                ''', locals())))

def parse_env_vars(env_vars):
    """Convert string descriptions of environment variable assignment into
    something that CrossEnvBuilder understands.

    :param env_vars:    An iterable of strings in the form 'FOO=BAR' or
                        'FOO?=BAR'
    :returns:           A list of (name, op, value)
    """

    parsed = []
    for spec in env_vars:
        spec = spec.lstrip()
        assign = '='
        try:
            name, value = spec.split('=',1)
        except IndexError:
            raise ValueError("Invalid variable %r. Must be in the form "
                              "NAME=VALUE or NAME?=VALUE" % spec)
        if name.endswith('?'):
            assign = '?='
            name = name[:-1]

        if not name.isidentifier():
            raise ValueError("Invalid variable name %r" % name)

        parsed.append((name, assign, value))
    return parsed


def main():
    import argparse
    parser = argparse.ArgumentParser(description="""
                Create virtual Python environments for cross compiling
                """)

    parser.add_argument('--cross-prefix', action='store',
        help="""Specify the directory where cross-python files will be stored.
                By default, this is within <ENV_DIR>/cross. You can override
                this to have host packages installed in an existing sysroot,
                for example. Watch out though: this will write to bin.""")
    parser.add_argument('--system-site-packages', action='store_true',
        help="""Give the *build* python environment access to the system
                site-packages dir.""")
    parser.add_argument('--clear', action='store_const', const='default',
        help="""Delete the contents of the environment directory if it already
                exists. This clears build-python, but cross-python will be
                cleared only if --cross-prefix was not set. See also
                --clear-both, --clear-cross, and --clear-build.""")
    parser.add_argument('--clear-cross', action='store_const', const='cross',
        dest='clear',
        help="""This clears cross-python only. See also --clear, --clear-both,
                and --clear-build.""")
    parser.add_argument('--clear-build', action='store_const', const='build',
        dest='clear',
        help="""This clears build-python only. See also --clear, --clear-both,
                and --clear-cross.""")
    parser.add_argument('--clear-both', action='store_const', const='both',
        dest='clear',
        help="""This clears both cross-python and build-python. See also
                --clear, --clear-both, and --clear-cross.""")
    parser.add_argument('--without-pip', action='store_true',
        help="""Skips installing or upgrading pip in both the build and cross
                virtual environments. (Pip is bootstrapped by default.)""")
    parser.add_argument('--without-build-pip', action='store_true',
        help="""Skips installing or upgrading pip the build virtual
                environments.""")
    parser.add_argument('--without-cross-pip', action='store_true',
        help="""Skips installing or upgrading pip in the cross virtual
                environment.""")
    parser.add_argument('--env', action='append', default=[],
        help="""An environment variable in the form FOO=BAR that will be
                added to the environment just before executing the python
                build executable. May be given multiple times. The form
                FOO?=BAR is also allowed to assign FOO only if not already
                set.""")
    parser.add_argument('-v', '--verbose', action='count', default=0,
        help="""Verbose mode. May be specified multiple times to increase
                verbosity.""")
    parser.add_argument('HOST_PYTHON',
        help="""The host Python to use. This should be the path to the Python
                executable, which may be in the source directory or an installed
                directory structure.""")
    parser.add_argument('ENV_DIR', nargs='+',
        help="""A directory to create the environment in.""")

    args = parser.parse_args()

    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose > 1:
        level = logging.DEBUG
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')

    try:
        if args.without_pip:
            args.without_cross_pip = True
            args.without_build_pip = True
        env = parse_env_vars(args.env)

        builder = CrossEnvBuilder(host_python=args.HOST_PYTHON,
                build_system_site_packages=args.system_site_packages,
                clear=args.clear,
                extra_env_vars=env,
                with_cross_pip=not args.without_cross_pip,
                with_build_pip=not args.without_build_pip,
                )
        for env_dir in args.ENV_DIR:
            builder.create(env_dir)
    except Exception as e:
        logger.error('%s', e)
        logger.debug('Traceback:', exc_info=True)

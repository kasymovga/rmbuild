
import pathlib
import tempfile
import contextlib
import hashlib
import subprocess
import logging
import os
import atexit
import shutil
# import distutils.dir_util
import threading
import queue

from .errors import *

_temp_dirs = []

hash_constructor = hashlib.md5
log = logging.getLogger(__name__)

QC_INSTALL_FILEEXT = ('.dat', '.lno')
GIT_EXECUTABLE = 'git'


@atexit.register
def cleanup():
    for tdir in _temp_dirs:
        log.debug('Removing temporary directory %r', tdir)
        shutil.rmtree(tdir, ignore_errors=True)


def directory(path):
    p = pathlib.Path(path)
    if not p.is_dir():
        raise PathError(p, "Not a directory")
    return p


def file(path):
    p = pathlib.Path(path)
    if not p.is_file():
        raise PathError(p, "Not a regular file")
    return p


def make_directory(path):
    os.makedirs(str(path), exist_ok=True)
    return directory(path)


def temp_directory():
    td = tempfile.mkdtemp(prefix='rmbuild')
    _temp_dirs.append(td)
    return directory(td)


@contextlib.contextmanager
def in_dir(path):
    old = pathlib.Path.cwd()
    os.chdir(str(path))
    yield
    os.chdir(str(old))


def read_in_chunks(fobj, chunksize=4096):
    return iter(lambda: fobj.read(chunksize), b'')


def hash_path(path, hashobject=None, root=None, namefilter=None):
    if root is None:
        root = path

    p = pathlib.Path(path)

    if hashobject is None:
        h = hash_constructor()
    else:
        h = hashobject

    name = p.relative_to(root).as_posix()

    if p.is_dir():
        name += "/"

    if namefilter is not None and not namefilter(name):
        return h

    h.update(name.encode('utf-8'))

    if path.is_dir():
        for fpath in path.iterdir():
            hash_path(fpath, hashobject=h, root=root, namefilter=namefilter)
    else:
        with open(str(p), 'rb') as f:
            for chunk in read_in_chunks(f):
                h.update(chunk)

    return h


def git(*args):
    return subprocess.check_output([GIT_EXECUTABLE] + list(args)).decode('utf-8').strip()


def namefilter_qcmodule(name):
    return not name.endswith('.log') and name != 'rm_auto.qh'


def logger(*name):
    return logging.getLogger('.'.join(name))


def logged_subprocess(popenargs, logger, log_level=logging.INFO, **kwargs):
    logger.debug("Invoking subprocess: %r", popenargs)

    child = subprocess.Popen(
        popenargs,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        **kwargs
    )

    for line in iter(child.stdout.readline, ''):
        line = line.strip()
        if line:
            logger.log(log_level, "[%s] %s" % (popenargs[0], line))

    code = child.wait()
    if code:
        raise subprocess.CalledProcessError(code, popenargs[0])


def copy_tree(src, dst):
    log.debug('copy_tree(): %r ---> %r', str(src), str(dst))

    # return distutils.dir_util.copy_tree(str(src), str(dst))

    from . import install
    index = install.build_index(src)
    install.copy_by_index(index, src, dst)


def copy(src, dst):
    log.debug('copy(): %r ---> %r', str(src), str(dst))
    shutil.copy(str(src), str(dst))


def clear_directory(path):
    path = directory(path)
    log.debug("Clearing directory %s", path)

    for fpath in path.iterdir():
        if fpath.is_file() or fpath.is_symlink():
            fpath.unlink()
        elif fpath.is_dir():
            shutil.rmtree(str(fpath), ignore_errors=True)

    assert not list(path.iterdir())


def daemon(name, func, start=True):
    thread = threading.Thread(target=func, name=name)
    thread.daemon = True

    if start:
        thread.start()

    return thread


class Worker(object):
    def __init__(self, name, threads=1, logger=log):
        self.name = name
        self.queue = queue.Queue()
        if threads > 1:
            self.threads = [daemon("%s:%i" % (name, i), self._func, start=False) for i in range(threads)]
        else:
            self.threads = [daemon(name, self._func, start=False)]
        self.log = logger
        self.started = False
        self.errors = []

    def start(self):
        for thread in self.threads:
            self._log("starting thread %r", thread)
            thread.start()

        self.started = True
        return self

    def wait(self):
        if self.started:
            for thread in self.threads:
                thread.join()

    def add_task(self, task):
        self._log("add_task (%r)" % task)
        r = self.queue.put(task)
        return r

    def _log(self, msg, *args):
        return self.log.debug("Worker %s: " + msg, self.name, *args)

    def _func(self):
        q = self.queue
        while True:
            if self.errors:
                break

            try:
                task = q.get(False)
            except queue.Empty:
                break

            try:
                task()
            except Exception as e:
                self.log.exception("Unhandled exception in a task for Worker %s", self.name)
                self.errors.append(e)
            q.task_done()
        self._log("thread terminating")


@contextlib.contextmanager
def suppress_logged(log, *ex):
    if not ex:
        ex = (Exception,)

    try:
        yield
    except ex:
        log.exception("Suppressed exception")

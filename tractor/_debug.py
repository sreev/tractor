"""
Multi-core debugging for da peeps!
"""
import pdb
import sys
import tty
import termios
from functools import partial
from typing import Awaitable, Tuple

from async_generator import aclosing
import tractor
import trio

from .log import get_logger

log = get_logger(__name__)


__all__ = ['breakpoint', 'post_mortem']


# TODO: is there some way to determine this programatically?
_pdb_exit_patterns = tuple(
    str.encode(patt + "\n") for patt in ('c', 'cont', 'continue', 'q', 'quit')
)


def subactoruid2proc(
    actor: 'Actor',  # noqa
    uid: Tuple[str, str]
) -> trio.Process:
    n = actor._actoruid2nursery[uid]
    _, proc, _ = n._children[uid]
    return proc


async def _hijack_stdin_relay_to_child(
    subactor_uid: Tuple[str, str]
) -> None:
    actor = tractor.current_actor()
    proc = subactoruid2proc(actor, subactor_uid)

    nlb = []
    line = []

    async def hijack_stdin():
        log.info(f"Hijacking stdin from {actor.uid}")
        try:
            # disable cooked mode
            fd = sys.stdin.fileno()
            old = tty.tcgetattr(fd)
            # tty.setcbreak(fd)
            tty.setcbreak(fd)

            # trap std in and relay to subproc
            async_stdin = trio.wrap_file(sys.stdin)

            async with aclosing(async_stdin):
                while True:
                # async for msg in async_stdin:
                    msg = await async_stdin.read(1)
                    nlb.append(msg)
                    # encode to bytes
                    bmsg = str.encode(msg)
                    log.trace(f"Stdin str input:\n{msg}")
                    log.trace(f"Stdin bytes input:\n{bmsg}")

                    # relay bytes to subproc over pipe
                    await proc.stdin.send_all(bmsg)
                    # termios.tcflush(fd, termios.TCIFLUSH)

                    # don't write control chars to local stdout
                    if bmsg not in (b'\t'):
                        # mirror input to stdout
                        sys.stdout.write(msg)
                        sys.stdout.flush()

                    if bmsg == b'\n':
                        line = str.encode(''.join(nlb))
                        # print(line.decode())
                        if line in _pdb_exit_patterns:
                            log.info("Closing stdin hijack")
                            line = []
                            break
        finally:
            tty.tcsetattr(fd, tty.TCSAFLUSH, old)

    # schedule hijacking in root scope
    actor._root_nursery.start_soon(hijack_stdin)


# XXX: We only make this sync in case someone wants to
# overload the ``breakpoint()`` built-in.
def _breakpoint(debug_func) -> Awaitable[None]:
    """``tractor`` breakpoint entry for engaging pdb machinery
    in subactors.
    """
    actor = tractor.current_actor()
    try:
        import ipdb
        db = ipdb
    except ImportError:
        import pdb
        db = pdb

    async def wait_for_parent_stdin_hijack():
        log.debug('Breakpoint engaged!')

        # TODO: need a more robust check for the "root" actor
        if actor._parent_chan:
            async with tractor._portal.open_portal(
                actor._parent_chan,
                start_msg_loop=False,
            ) as portal:
                # with trio.fail_after(1):
                await portal.run(
                    'tractor._debug',
                    '_hijack_stdin_relay_to_child',
                    subactor_uid=actor.uid,
                )

        # block here one frame up where ``breakpoint()``
        # was awaited and begin handling stdin
        debug_func(actor, db)

    # this must be awaited by caller
    return wait_for_parent_stdin_hijack()


def _set_trace(actor, dbmod):
    dbmod.set_trace(
        header=f"\nAttaching pdb to actor: {actor.uid}\n",
        # start 2 levels up
        frame=sys._getframe().f_back.f_back,
    )


breakpoint = partial(
    _breakpoint,
    _set_trace,
)


def _post_mortem(actor, dbmod):
    log.error(
        f"\nAttaching to {dbmod} in crashed actor: {actor.uid}\n")
    dbmod.post_mortem()


post_mortem = partial(
    _breakpoint,
    _post_mortem,
)
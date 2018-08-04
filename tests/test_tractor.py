"""
Actor model API testing
"""
import time
from functools import partial, wraps
from itertools import repeat
import random

import pytest
import trio
import tractor


_arb_addr = '127.0.0.1', random.randint(1000, 9999)


def tractor_test(fn):
    """
    Use:

    @tractor_test
    async def test_whatever():
        await ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        __tracebackhide__ = True
        return tractor.run(
            partial(fn, *args), arbiter_addr=_arb_addr, **kwargs)

    return wrapper


@pytest.mark.trio
async def test_no_arbitter():
    """An arbitter must be established before any nurseries
    can be created.

    (In other words ``tractor.run`` must be used instead of ``trio.run`` as is
    done by the ``pytest-trio`` plugin.)
    """
    with pytest.raises(RuntimeError):
        with tractor.open_nursery():
            pass


def test_local_actor_async_func():
    """Verify a simple async function in-process.
    """
    nums = []

    async def print_loop():
        # arbiter is started in-proc if dne
        assert tractor.current_actor().is_arbiter

        for i in range(10):
            nums.append(i)
            await trio.sleep(0.1)

    start = time.time()
    tractor.run(print_loop, arbiter_addr=_arb_addr)

    # ensure the sleeps were actually awaited
    assert time.time() - start >= 1
    assert nums == list(range(10))


statespace = {'doggy': 10, 'kitty': 4}


# NOTE: this func must be defined at module level in order for the
# interal pickling infra of the forkserver to work
async def spawn(is_arbiter):
    namespaces = [__name__]

    await trio.sleep(0.1)
    actor = tractor.current_actor()
    assert actor.is_arbiter == is_arbiter
    assert actor.statespace == statespace

    if actor.is_arbiter:
        async with tractor.open_nursery() as nursery:
            # forks here
            portal = await nursery.run_in_actor(
                'sub-actor',
                spawn,
                is_arbiter=False,
                statespace=statespace,
                rpc_module_paths=namespaces,
            )

            assert len(nursery._children) == 1
            assert portal.channel.uid in tractor.current_actor()._peers
            # be sure we can still get the result
            result = await portal.result()
            assert result == 10
            return result
    else:
        return 10


def test_local_arbiter_subactor_global_state():
    result = tractor.run(
        spawn,
        True,
        name='arbiter',
        statespace=statespace,
        arbiter_addr=_arb_addr,
    )
    assert result == 10


async def stream_seq(sequence):
    for i in sequence:
        yield i
        await trio.sleep(0.1)


async def stream_from_single_subactor():
    """Verify we can spawn a daemon actor and retrieve streamed data.
    """
    async with tractor.find_actor('brokerd') as portals:
        if not portals:
            # only one per host address, spawns an actor if None
            async with tractor.open_nursery() as nursery:
                # no brokerd actor found
                portal = await nursery.start_actor(
                    'streamerd',
                    rpc_module_paths=[__name__],
                    statespace={'global_dict': {}},
                )

                seq = range(10)

                agen = await portal.run(
                    __name__,
                    'stream_seq',  # the func above
                    sequence=list(seq),  # has to be msgpack serializable
                )
                # it'd sure be nice to have an asyncitertools here...
                iseq = iter(seq)
                async for val in agen:
                    assert val == next(iseq)
                    # TODO: test breaking the loop (should it kill the
                    # far end?)
                    # break
                    # terminate far-end async-gen
                    # await gen.asend(None)
                    # break

                # stop all spawned subactors
                await portal.cancel_actor()
                # await nursery.cancel()


def test_stream_from_single_subactor():
    """Verify streaming from a spawned async generator.
    """
    tractor.run(stream_from_single_subactor, arbiter_addr=_arb_addr)


async def assert_err():
    assert 0


def test_remote_error():
    """Verify an error raises in a subactor is propagated to the parent.
    """
    async def main():
        async with tractor.open_nursery() as nursery:

            portal = await nursery.run_in_actor('errorer', assert_err)

            # get result(s) from main task
            try:
                return await portal.result()
            except tractor.RemoteActorError:
                print("Look Maa that actor failed hard, hehh")
                raise
            except Exception:
                pass
            assert 0, "Remote error was not raised?"

    with pytest.raises(tractor.RemoteActorError):
        # also raises
        tractor.run(main, arbiter_addr=_arb_addr)


async def stream_forever():
    for i in repeat("I can see these little future bubble things"):
        yield i
        await trio.sleep(0.01)


@tractor_test
async def test_cancel_infinite_streamer():

    # stream for at most 5 seconds
    with trio.move_on_after(1) as cancel_scope:
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                f'donny',
                rpc_module_paths=[__name__],
            )
            async for letter in await portal.run(__name__, 'stream_forever'):
                print(letter)

    assert cancel_scope.cancelled_caught
    assert n.cancelled


@tractor_test
async def test_one_cancels_all():
    """Verify one failed actor causes all others in the nursery
    to be cancelled just like in trio.

    This is the first and only supervisory strategy at the moment.
    """
    try:
        async with tractor.open_nursery() as n:
            real_actors = []
            for i in range(3):
                real_actors.append(await n.start_actor(
                    f'actor_{i}',
                    rpc_module_paths=[__name__],
                ))

            # start one actor that will fail immediately
            await n.run_in_actor('extra', assert_err)

        # should error here with a ``RemoteActorError`` containing
        # an ``AssertionError`

    except tractor.RemoteActorError:
        assert n.cancelled is True
        assert not n._children
    else:
        pytest.fail("Should have gotten a remote assertion error?")


the_line = 'Hi my name is {}'


async def hi():
    return the_line.format(tractor.current_actor().name)


async def say_hello(other_actor):
    await trio.sleep(0.4)  # wait for other actor to spawn
    async with tractor.find_actor(other_actor) as portal:
        return await portal.run(__name__, 'hi')


@tractor_test
async def test_trynamic_trio():
    """Main tractor entry point, the "master" process (for now
    acts as the "director").
    """
    async with tractor.open_nursery() as n:
        print("Alright... Action!")

        donny = await n.run_in_actor(
            'donny',
            say_hello,
            other_actor='gretchen',
        )
        gretchen = await n.run_in_actor(
            'gretchen',
            say_hello,
            other_actor='donny',
        )
        print(await gretchen.result())
        print(await donny.result())
        await donny.cancel_actor()
        print("CUTTTT CUUTT CUT!!?! Donny!! You're supposed to say...")


def movie_theatre_question():
    """A question asked in a dark theatre, in a tangent
    (errr, I mean different) process.
    """
    return 'have you ever seen a portal?'


@tractor_test
async def test_movie_theatre_convo():
    """The main ``tractor`` routine.
    """
    async with tractor.open_nursery() as n:
        portal = await n.start_actor(
            'frank',
            # enable the actor to run funcs from this current module
            rpc_module_paths=[__name__],
        )

        print(await portal.run(__name__, 'movie_theatre_question'))
        # calls the subactor a 2nd time
        print(await portal.run(__name__, 'movie_theatre_question'))

        # the async with will block here indefinitely waiting
        # for our actor "frank" to complete, we cancel 'frank'
        # to avoid blocking indefinitely
        await portal.cancel_actor()


@tractor_test
async def test_movie_theatre_convo_main_task():
    async with tractor.open_nursery() as n:
        portal = await n.run_in_actor('frank', movie_theatre_question)

    # The ``async with`` will unblock here since the 'frank'
    # actor has completed its main task ``movie_theatre_question()``.

    print(await portal.result())


def cellar_door():
    return "Dang that's beautiful"


@tractor_test
async def test_most_beautiful_word():
    """The main ``tractor`` routine.
    """
    async with tractor.open_nursery() as n:
        portal = await n.run_in_actor('some_linguist', cellar_door)

    # The ``async with`` will unblock here since the 'some_linguist'
    # actor has completed its main task ``cellar_door``.

    print(await portal.result())


def do_nothing():
    pass


def test_cancel_single_subactor():

    async def main():

        async with tractor.open_nursery() as nursery:

            portal = await nursery.start_actor(
                'nothin', rpc_module_paths=[__name__],
            )
            assert (await portal.run(__name__, 'do_nothing')) is None

            # would hang otherwise
            await nursery.cancel()

    tractor.run(main, arbiter_addr=_arb_addr)


async def stream_data(seed):
    for i in range(seed):
        yield i
        await trio.sleep(0)  # trigger scheduler


async def aggregate(seed):
    """Ensure that the two streams we receive match but only stream
    a single set of values to the parent.
    """
    async with tractor.open_nursery() as nursery:
        portals = []
        for i in range(1, 3):
            # fork point
            portal = await nursery.start_actor(
                name=f'streamer_{i}',
                rpc_module_paths=[__name__],
            )

            portals.append(portal)

        q = trio.Queue(500)

        async def push_to_q(portal):
            async for value in await portal.run(
                __name__, 'stream_data', seed=seed
            ):
                # leverage trio's built-in backpressure
                await q.put(value)

            await q.put(None)
            print(f"FINISHED ITERATING {portal.channel.uid}")

        # spawn 2 trio tasks to collect streams and push to a local queue
        async with trio.open_nursery() as n:
            for portal in portals:
                n.start_soon(push_to_q, portal)

            unique_vals = set()
            async for value in q:
                if value not in unique_vals:
                    unique_vals.add(value)
                    # yield upwards to the spawning parent actor
                    yield value

                    if value is None:
                        break

                assert value in unique_vals

            print("FINISHED ITERATING in aggregator")

        await nursery.cancel()
        print("WAITING on `ActorNursery` to finish")
    print("AGGREGATOR COMPLETE!")


async def a_quadruple_example():
    # a nursery which spawns "actors"
    async with tractor.open_nursery() as nursery:

        seed = int(1e3)
        pre_start = time.time()

        portal = await nursery.run_in_actor(
            'aggregator',
            aggregate,
            seed=seed,
        )

        start = time.time()
        # the portal call returns exactly what you'd expect
        # as if the remote "main" function was called locally
        result_stream = []
        async for value in await portal.result():
            result_stream.append(value)

        print(f"STREAM TIME = {time.time() - start}")
        print(f"STREAM + SPAWN TIME = {time.time() - pre_start}")
        assert result_stream == list(range(seed)) + [None]
        return result_stream


async def cancel_after(wait):
    with trio.move_on_after(wait):
        return await a_quadruple_example()


def test_a_quadruple_example():
    """This also serves as a kind of "we'd like to eventually be this
    fast test".
    """
    results = tractor.run(cancel_after, 2.1, arbiter_addr=_arb_addr)
    assert results


@pytest.mark.parametrize('cancel_delay', list(range(1, 7)))
def test_not_fast_enough_quad(cancel_delay):
    """Verify we can cancel midway through the quad example and all actors
    cancel gracefully.
    """
    delay = 1 + cancel_delay/10
    results = tractor.run(cancel_after, delay, arbiter_addr=_arb_addr)
    assert results is None

import tractor


async def name_error():
    "Raise a ``NameError``"
    getattr(doggypants)


async def spawn_error():
    """"A nested nursery that triggers another ``NameError``.
    """
    async with tractor.open_nursery() as n:
        portal = await n.run_in_actor('name_error_1', name_error)
        return await portal.result()


async def main():
    """The main ``tractor`` routine.

    The process tree should look as approximately as follows:

    python examples/debugging/multi_subactors.py
    ├─ python -m tractor._child --uid ('name_error', 'a7caf490 ...)
    `-python -m tractor._child --uid ('spawn_error', '52ee14a5 ...)
       `-python -m tractor._child --uid ('name_error', '3391222c ...)
    """
    async with tractor.open_nursery() as n:

        # spawn both actors
        portal = await n.run_in_actor('name_error', name_error)
        portal1 = await n.run_in_actor('spawn_error', spawn_error)

        # trigger a root actor error
        assert 0

        # attempt to collect results (which raises error in parent)
        # still has some issues where the parent seems to get stuck
        await portal.result()
        await portal1.result()


if __name__ == '__main__':
    tractor.run(main, debug_mode=True)

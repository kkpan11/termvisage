"""Definitions for deferred image rendering"""

from __future__ import annotations

import logging as _logging
from collections import defaultdict
from multiprocessing import Event as mp_Event, Lock as mp_Lock, Queue as mp_Queue
from os import remove
from os.path import split
from queue import Empty, Queue
from tempfile import mkdtemp, mkstemp
from threading import Event, Lock
from typing import Union

from term_image.image import Size

from .. import logging, notify
from ..logging_multi import Process
from ..utils import clear_queue
from . import main


def delete_thumbnail(thumbnail: str) -> bool:
    try:
        remove(thumbnail)
    except OSError:  # On Windows, a file in use cannot be deleted
        logging.log_exception(f"Failed to delete thumbnail file {thumbnail!r}", logger)
        return False

    return True


def refresh_grid_rendering() -> None:
    if main.THUMBNAIL:
        grid_thumbnail_queue.put(None)  # Send the grid delimiter
        grid_thumbnailer_in_sync.clear()
        grid_thumbnailer_in_sync.wait()

    grid_render_queue.put(None)  # Send the grid delimiter
    grid_renderer_in_sync.clear()
    grid_renderer_in_sync.wait()


def generate_grid_thumbnails(
    input: Queue | mp_Queue,
    output: Queue | mp_Queue,
    thumbnail_size: int,
    not_generating: Event | mp_Event,
    deduplication_lock: Lock | mp_Lock,
) -> None:
    from glob import iglob
    from os import fdopen, mkdir, scandir
    from shutil import copyfile, rmtree
    from sys import hash_info

    from PIL.Image import Resampling, open as Image_open

    THUMBNAIL_FRAME_SIZE = (thumbnail_size,) * 2
    BOX = Resampling.BOX
    THUMBNAIL_MODES = {"RGB", "RGBA"}
    # No of nibbles (hex digits) in the platform-specific hash integer type
    HEX_HASH_WIDTH = hash_info.width // 4  # 4 bits -> 1 hex digit
    # The max value for the unsigned counterpart of the platform-specific hash
    # integer type
    UINT_HASH_WIDTH_MAX = (1 << hash_info.width) - 1

    deduplicated_to_be_deleted: set[str] = set()

    try:
        THUMBNAIL_DIR = (TEMP_DIR := mkdtemp(prefix="termvisage-")) + "/thumbnails"
        mkdir(THUMBNAIL_DIR)
    except OSError:
        logging.log_exception(
            "Failed to create thumbnail directory", logger, fatal=True
        )
        raise

    logging.log(
        f"Created thumbnail directory {THUMBNAIL_DIR!r}",
        logger,
        _logging.DEBUG,
        direct=False,
    )

    try:
        while True:
            not_generating.set()
            try:
                if not (source := input.get()):
                    break  # Quitting
            finally:
                not_generating.clear()

            if deduplicated_to_be_deleted:
                # Retain only the files that still exist;
                # remove files that have been deleted.
                deduplicated_to_be_deleted.intersection_update(
                    entry.path for entry in scandir(THUMBNAIL_DIR)
                )

            # Make source image into a thumbnail
            try:
                img = Image_open(source)
                has_transparency = img.has_transparency_data
                if img.mode not in THUMBNAIL_MODES:
                    with img:
                        img = img.convert("RGBA" if has_transparency else "RGB")
                img.thumbnail(THUMBNAIL_FRAME_SIZE, BOX)
            except Exception:
                output.put((source, None, None))
                logging.log_exception(
                    f"Failed to generate thumbnail for {source!r}", logger
                )
                continue

            img_bytes = img.tobytes()
            # The hash is interpreted as an unsigned integer, represented in hex and
            # zero-extended to fill up the platform-specific hash integer width.
            img_hash = f"{hash(img_bytes) & UINT_HASH_WIDTH_MAX:0{HEX_HASH_WIDTH}x}"

            # Create thumbnail file
            try:
                thumbnail_fd, thumbnail = mkstemp("", f"{img_hash}-", THUMBNAIL_DIR)
            except Exception:
                output.put((source, None, None))
                logging.log_exception(
                    f"Failed to create thumbnail file for {source!r}", logger
                )
                del img_bytes  # Possibly relatively large
                img.close()
                continue

            # Deduplication
            deduplicated = None
            with deduplication_lock:
                for other_thumbnail in iglob(
                    f"{THUMBNAIL_DIR}/{img_hash}-*", root_dir=THUMBNAIL_DIR
                ):
                    if (
                        other_thumbnail == thumbnail
                        or other_thumbnail in deduplicated_to_be_deleted
                    ):
                        continue

                    with Image_open(other_thumbnail) as other_img:
                        if other_img.tobytes() != img_bytes:
                            continue

                    try:
                        copyfile(other_thumbnail, thumbnail)
                    except Exception:
                        logging.log_exception(
                            f"Failed to deduplicate {other_thumbnail!r} for "
                            "{source!r}",
                            logger,
                        )
                    else:
                        deduplicated_to_be_deleted.add(deduplicated := other_thumbnail)

                    break

            del img_bytes  # Possibly relatively large

            # Save thumbnail, if deduplication didn't work out
            if not deduplicated:
                with img, fdopen(thumbnail_fd, "wb") as thumbnail_file:
                    try:
                        img.save(thumbnail_file, "PNG")
                    except Exception:
                        output.put((source, None, None))
                        thumbnail_file.close()  # Close before deleting the file
                        delete_thumbnail(thumbnail)
                        logging.log_exception(
                            f"Failed to save thumbnail for {source!r}", logger
                        )
                        continue

            output.put((source, thumbnail, deduplicated))
    finally:
        try:
            rmtree(TEMP_DIR, ignore_errors=True)
        except OSError:
            logging.log_exception(
                f"Failed to delete thumbnail directory {THUMBNAIL_DIR!r}", logger
            )

    clear_queue(output)


def manage_anim_renders() -> None:
    from .main import ImageClass, update_screen
    from .widgets import ImageCanvas, image_box

    def next_frame() -> bool:
        frame, repeat, frame_no, size, rendered_size = frame_render_out.get()
        if not_skip() and (not forced or image_w._ti_force_render):
            if frame:
                canv = ImageCanvas(frame.encode().split(b"\n"), size, rendered_size)
                image_w._ti_image.seek(frame_no)
                image_w._ti_frame = (canv, repeat, frame_no)
            else:
                image_w._ti_anim_finished = True
                image_w._ti_image.seek(0)
        # If this image is the one currently displayed, it's either:
        # - forced but size changed -> End animation; Remove attributes
        # - a size change -> Continue animation; Do not remove attributes
        # - a restart (moved to another entry and back) -> Animation will be ended at
        #   restart; attributes already removed in `.main.animate_image()`
        elif image_w is not image_box.original_widget or forced:
            frame_render_in.put((..., None, None))
            clear_queue(frame_render_out)  # In case output is full
            frame = None

        if not frame:
            try:
                # If one fails, the rest shouldn't exist (removed in `animate_image()`)
                del image_w._ti_anim_ongoing
                del image_w._ti_frame
                if forced:
                    # See "Forced render" section of `.widgets.Image.render()`
                    del image_w._ti_force_render
                    del image_w._ti_forced_anim_size_hash
            except AttributeError:
                pass

        update_screen()
        return bool(frame)

    def not_skip():
        return image_w is image_box.original_widget and anim_render_queue.empty()

    frame_render_in = (mp_Queue if logging.MULTI else Queue)()
    frame_render_out = (mp_Queue if logging.MULTI else Queue)(20)
    ready = (mp_Event if logging.MULTI else Event)()
    renderer = (Process if logging.MULTI else logging.Thread)(
        target=render_frames,
        args=(
            frame_render_in,
            frame_render_out,
            ready,
            ImageClass,
            anim_style_specs.get(ImageClass.style, ""),
            REPEAT,
            ANIM_CACHED,
        ),
        name="FrameRenderer",
        redirect_notifs=True,
    )
    renderer.start()

    frame_duration = None
    image_w = None  # Silence flake8's F821

    try:
        while True:
            try:
                data, size, forced = anim_render_queue.get(timeout=frame_duration)
            except Empty:
                if not next_frame():
                    frame_duration = None
            else:
                if not data:
                    break

                notify.start_loading()

                if anim_render_queue.empty():
                    ready.clear()
                    frame_render_in.put((..., None, None))
                    clear_queue(frame_render_out)  # In case output is full
                    ready.wait()
                    # multiprocessing queues are not so reliable
                    clear_queue(frame_render_out)

                if isinstance(data, tuple):
                    if not_skip():
                        frame_render_in.put((data, size, image_w._ti_alpha))
                        if not next_frame():
                            frame_duration = None
                    elif image_w is not image_box.original_widget:
                        # The next item in the queue is NOT a size change
                        frame_duration = None
                else:
                    # Safe, since the next item in the queue cannot be a size change
                    # cos no animation is ongoing
                    frame_duration = None

                    image_w = data
                    if not_skip():
                        frame_render_in.put(
                            (image_w._ti_image._source, size, image_w._ti_alpha)
                        )
                        # Ensures successful deletion if the displayed image has
                        # changed before the first frame is ready
                        image_w._ti_frame = None

                        if next_frame():
                            frame_duration = (
                                FRAME_DURATION or image_w._ti_image.frame_duration
                            )

                notify.stop_loading()
    finally:
        clear_queue(frame_render_in)
        frame_render_in.put((None,) * 3)
        clear_queue(frame_render_out)  # In case the renderer is blocking on `put()`
        renderer.join()
        clear_queue(anim_render_queue)


def manage_image_renders():
    from .main import ImageClass, update_screen
    from .widgets import Image, ImageCanvas, image_box

    def not_skip():
        # If this image is the one currently displayed but the queue is non empty,
        # it means some "forth and back" has occurred.
        # Skipping this render avoids the possibility of wasting time with this render
        # in the case where the image size has changed.
        return image_w is image_box.original_widget and image_render_queue.empty()

    multi = logging.MULTI
    image_render_in = (mp_Queue if multi else Queue)()
    image_render_out = (mp_Queue if multi else Queue)()
    renderer = (Process if multi else logging.Thread)(
        target=render_images,
        args=(
            image_render_in,
            image_render_out,
            ImageClass,
            image_style_specs.get(ImageClass.style, ""),
        ),
        name="ImageRenderer",
        redirect_notifs=True,
    )
    renderer.start()

    faulty_image = Image._ti_faulty_image
    last_image_w = image_box.original_widget
    # To prevent an `AttributeError` with the first deletion, while avoiding `hasattr()`
    last_image_w._ti_canv = None

    try:
        while True:
            # A redraw is necessary even when the render is skipped, in case the skipped
            # render is of the currently displayed image.
            # So that a new render can be sent in (after `._ti_rendering` is unset).
            # Otherwise, the image will remain unrendered until a redraw.
            update_screen()

            image_w, size, alpha = image_render_queue.get()
            if not image_w:
                break

            if not not_skip():
                del image_w._ti_rendering
                continue

            image_render_in.put(
                (
                    image_w._ti_image._source,
                    size,
                    alpha,
                    image_w._ti_faulty,
                )
            )
            notify.start_loading()
            render, rendered_size = image_render_out.get()

            if not_skip():
                del last_image_w._ti_canv
                if render:
                    image_w._ti_canv = ImageCanvas(
                        render.encode().split(b"\n"), size, rendered_size
                    )
                else:
                    image_w._ti_canv = faulty_image.render(size)
                    # Ensures a fault is logged only once per `Image` instance
                    if not image_w._ti_faulty:
                        image_w._ti_faulty = True
                last_image_w = image_w

            del image_w._ti_rendering
            notify.stop_loading()
    finally:
        clear_queue(image_render_in)
        image_render_in.put((None,) * 4)
        renderer.join()
        clear_queue(image_render_queue)


def manage_grid_renders(n_renderers: int):
    """Manages grid cell rendering.

    Intended to be executed in a separate thread of the main process.

    If multiprocessing is enabled and *n_renderers* > 0, it spawns *n_renderers*
    subprocesses to render the cells and handles their proper termination.
    Otherwise, it starts a single new thread to render the cells.
    """
    from . import main
    from .main import ImageClass, grid_active, quitting, update_screen
    from .widgets import Image, ImageCanvas, image_grid

    # NOTE:
    # Always keep in mind that every directory entry is rendered only once per grid
    # since results are cached, at least for now.

    def mark_thumbnail_rendered(source: str, thumbnail: str) -> None:
        with thumbnail_render_lock:
            if len(sources := thumbnails_being_rendered[thumbnail]) == 1:
                del thumbnails_being_rendered[thumbnail]
            else:
                sources.remove(source)
            # *source* may be in both caches in the case of thumbnail deduplication.
            # In such a case, since a source is never rendered more than once
            # between grid render syncs, then *thumbnail* is the deduplicated thumbnail
            # in the extra cache.
            if source in extra_thumbnail_cache:
                # Safe to remove since every source is never rendered more than once
                # between grid render syncs.
                # Need to remove at this point because afterwards, there's no efficient
                # way to tie `thumbnail` to `source`.
                del extra_thumbnail_cache[source]

    multi = logging.MULTI and n_renderers > 0
    grid_render_in = (mp_Queue if multi else Queue)()
    grid_render_out = (mp_Queue if multi else Queue)()
    renderers = [
        (Process if multi else logging.Thread)(
            target=render_grid_images,
            args=(
                grid_render_in,
                grid_render_out,
                ImageClass,
                Image._ti_alpha,
                grid_style_specs.get(ImageClass.style, ""),
            ),
            name="GridRenderer" + f"-{n}" * multi,
            redirect_notifs=True,
        )
        for n in range(n_renderers if multi else 1)
    ]
    for renderer in renderers:
        renderer.start()

    grid_image_size = grid_path = None  # Silence flake8's F821
    faulty_image = Image._ti_faulty_image
    delimited = False
    grid_cache = Image._ti_grid_cache
    in_sync = grid_renderer_in_sync

    try:
        while True:
            while not (
                grid_active.wait(0.1)
                or quitting.is_set()
                or not grid_render_out.empty()
            ):
                pass
            if quitting.is_set():
                break

            if delimited or not in_sync.is_set():
                grid_cache.clear()
                in_sync.set()

                # Purge the in and out queues and update the loading indicator counter
                for q in (grid_render_in, grid_render_out):
                    while True:
                        try:
                            q.get(timeout=0.005)
                        except Empty:
                            break
                        else:
                            notify.stop_loading()

                if not delimited:
                    # Purge all items until the grid delimiter
                    while grid_render_queue.get():
                        pass
                else:
                    delimited = False

                grid_path = main.grid_path
                grid_cell_width = image_grid.cell_width
                grid_image_size = (grid_cell_width - 2, grid_cell_width // 2 - 2)
                del grid_cell_width

            if not in_sync.is_set():
                continue

            if grid_active.is_set():
                try:
                    source_and_thumbnail = grid_render_queue.get(timeout=0.02)
                except Empty:
                    pass
                else:
                    if not source_and_thumbnail:
                        delimited = True
                        continue
                    grid_render_in.put((*source_and_thumbnail, grid_image_size))
                    notify.start_loading()

            if not in_sync.is_set():
                continue

            try:
                source, thumbnail, render, canvas_size, rendered_size = (
                    grid_render_out.get(timeout=0.02)
                )
            except Empty:
                pass
            else:
                source_dirname, source_basename = split(source)
                if (
                    in_sync.is_set()
                    # The directory and cell-width checks are to filter out any
                    # remnants that were caught up in the renderer(s) while syncing
                    # grid rendering.
                    and source_dirname == grid_path
                    and canvas_size == grid_image_size
                ):
                    grid_cache[source_basename] = (
                        ImageCanvas(
                            render.encode().split(b"\n"), canvas_size, rendered_size
                        )
                        if render
                        else faulty_image.render(canvas_size)
                    )
                    if grid_active.is_set():
                        update_screen()

                    # There's no need to check `.tui.main.THUMBNAIL` since
                    # `thumbnail` is always `None` when thumbnailing is disabled.
                    if thumbnail:
                        mark_thumbnail_rendered(source, thumbnail)

                notify.stop_loading()
    finally:
        clear_queue(grid_render_in)
        for renderer in renderers:
            grid_render_in.put((None,) * 3)
        for renderer in renderers:
            renderer.join()
        clear_queue(grid_render_queue)


def manage_grid_thumbnails(thumbnail_size: int) -> None:
    from .main import grid_active, quitting

    # NOTE:
    # Always keep in mind that every directory entry is rendered only once per grid
    # since results are cached, at least for now.

    def cache_thumbnail(source: str, thumbnail: str, deduplicated: str | None) -> None:
        # Eviction, for finite cache size
        if not deduplicated and 0 < THUMBNAIL_CACHE_SIZE == len(thumbnail_sources):
            # Evict the oldest thumbnail with the least amount of linked sources.
            other_thumbnail = min(
                thumbnail_sources,
                key=lambda thumbnail: len(thumbnail_sources[thumbnail]),
            )
            # `thumbnail_render_lock` is unnecessary for just a membership test on
            # `thumbnails_being_rendered`; the outcome is the same as with the lock
            # but without is less costly.
            if other_thumbnail in thumbnails_being_rendered:
                with thumbnail_render_lock:
                    # Copy only the linked sources that are in the render pipeline
                    # into the extra cache.
                    for other_source in thumbnails_being_rendered[other_thumbnail]:
                        extra_thumbnail_cache[other_source] = other_thumbnail
                    # Remove all linked sources from the main cache.
                    for other_source in thumbnail_sources[other_thumbnail]:
                        del thumbnail_cache[other_source]
                # Queue it up to be deleted later.
                thumbnails_to_be_deleted.add(other_thumbnail)
            else:
                with deduplication_lock:
                    delete_thumbnail(other_thumbnail)
                for other_source in thumbnail_sources[other_thumbnail]:
                    # `thumbnail_render_lock` is unnecessary here since
                    # `other_thumbnail` is not in the render pipeline.
                    del thumbnail_cache[other_source]
            del thumbnail_sources[other_thumbnail]

        thumbnail_cache[source] = thumbnail  # Link *source* to *thumbnail*.

        # Deduplication
        if deduplicated and (
            # has the deduplicated thumbnail NOT been evicted? (next two lines)
            not THUMBNAIL_CACHE_SIZE
            or deduplicated in thumbnail_sources
        ):
            # Unlink *deduplicated* from the sources linked to it and link *thumbnail*
            # to them, along with *source*.
            deduplicated_sources = thumbnail_sources.pop(deduplicated)
            thumbnail_sources[thumbnail] = (*deduplicated_sources, source)

            with thumbnail_render_lock:
                # Link to *thumbnail*, the sources linked to *deduplicated*.
                for deduplicated_source in deduplicated_sources:
                    thumbnail_cache[deduplicated_source] = thumbnail

                if deduplicated in thumbnails_being_rendered:
                    # Copy sources linked to *deduplicated* and in the render pipeline
                    # into the extra cache.
                    for deduplicated_source in thumbnails_being_rendered[deduplicated]:
                        extra_thumbnail_cache[deduplicated_source] = deduplicated
                    # Queue *deduplicated* up to be deleted later.
                    thumbnails_to_be_deleted.add(deduplicated)
                else:
                    with deduplication_lock:
                        delete_thumbnail(deduplicated)
        else:
            thumbnail_sources[thumbnail] = (source,)

    multi = logging.MULTI
    thumbnail_in = (mp_Queue if multi else Queue)()
    thumbnail_out = (mp_Queue if multi else Queue)()
    not_generating = (mp_Event if multi else Event)()
    deduplication_lock = (mp_Lock if multi else Lock)()
    generator = (Process if multi else logging.Thread)(
        target=generate_grid_thumbnails,
        args=(
            thumbnail_in,
            thumbnail_out,
            thumbnail_size,
            not_generating,
            deduplication_lock,
        ),
        name="GridThumbnailer",
        redirect_notifs=True,
    )
    generator.start()
    not_generating.set()

    delimited = False
    in_sync = grid_thumbnailer_in_sync
    renderer_in_sync = grid_renderer_in_sync
    thumbnails_to_be_deleted: set[str] = set()

    try:
        while True:
            while not (
                grid_active.wait(0.1)
                or quitting.is_set()
                or not thumbnail_out.empty()
                or thumbnails_to_be_deleted
            ):
                pass
            if quitting.is_set():
                break

            if delimited or not in_sync.is_set():
                in_sync.set()
                renderer_in_sync.wait()

                with thumbnail_render_lock:
                    extra_thumbnail_cache.clear()
                    thumbnails_being_rendered.clear()

                # Purge the in queue and update the loading indicator counter
                while True:
                    try:
                        thumbnail_in.get(timeout=0.005)
                    except Empty:
                        break
                    else:
                        notify.stop_loading()

                # Wait for the thumbnail being generated, if any
                not_generating.wait()

                # Cache or delete already generated thumbnails in the out queue
                # and update the loading indicator counter
                while True:
                    try:
                        source, thumbnail, deduplicated = thumbnail_out.get(
                            timeout=0.005
                        )
                    except Empty:
                        break
                    else:
                        if not deduplicated and (
                            0 < THUMBNAIL_CACHE_SIZE == len(thumbnail_sources)
                        ):
                            # Quicker than the eviction process
                            delete_thumbnail(thumbnail)
                        else:
                            cache_thumbnail(source, thumbnail, deduplicated)
                        notify.stop_loading()

                for thumbnail in thumbnails_to_be_deleted:
                    delete_thumbnail(thumbnail)
                thumbnails_to_be_deleted.clear()

                if not delimited:
                    while grid_thumbnail_queue.get():
                        pass
                else:
                    delimited = False

            if not in_sync.is_set():
                continue

            if thumbnails_to_be_deleted:
                # `thumbnail_render_lock` is unnecessary here since
                # `thumbnails_being_rendered` is only **read** just **once**; the
                # outcome is the same as with the lock but without is less costly.
                thumbnails_to_delete = (
                    thumbnails_to_be_deleted - thumbnails_being_rendered.keys()
                )
                for thumbnail in thumbnails_to_delete:
                    with deduplication_lock:
                        delete_thumbnail(thumbnail)
                thumbnails_to_be_deleted -= thumbnails_to_delete

            if not in_sync.is_set():
                continue

            if grid_active.is_set():
                try:
                    source_and_thumbnail = grid_thumbnail_queue.get(timeout=0.02)
                except Empty:
                    pass
                else:
                    if not source_and_thumbnail:
                        delimited = True
                        continue
                    if thumbnail := thumbnail_cache.get(
                        source := source_and_thumbnail[0]
                    ):
                        grid_render_queue.put((source, thumbnail))
                        with thumbnail_render_lock:
                            thumbnails_being_rendered[thumbnail].add(source)
                    else:
                        thumbnail_in.put(source)
                        notify.start_loading()

            if not in_sync.is_set():
                continue

            try:
                source, thumbnail, deduplicated = thumbnail_out.get(timeout=0.02)
            except Empty:
                pass
            else:
                if in_sync.is_set():
                    grid_render_queue.put((source, thumbnail))
                    if thumbnail:
                        with thumbnail_render_lock:
                            thumbnails_being_rendered[thumbnail].add(source)
                if thumbnail:
                    cache_thumbnail(source, thumbnail, deduplicated)
                notify.stop_loading()
    finally:
        clear_queue(thumbnail_in)
        thumbnail_in.put(None)
        generator.join()
        clear_queue(grid_thumbnail_queue)


def render_frames(
    input: Union[Queue, mp_Queue],
    output: Union[Queue, mp_Queue],
    ready: Union[Event, mp_Event],
    ImageClass: type,
    style_spec: str,
    repeat: int,
    cached: Union[bool, int],
):
    """Renders animation frames.

    Intended to be executed in a subprocess or thread.
    """
    from term_image.image import ImageIterator

    image = animator = None  # Silence flake8's F821
    block = True
    while True:
        try:
            data, size, alpha = input.get(block)
        except Empty:
            try:
                output.put(
                    (
                        next(animator),
                        animator.loop_no,
                        image.tell(),
                        size,
                        image.rendered_size,
                    )
                )
            except StopIteration:
                output.put((None,) * 5)
                block = True
            except Exception as e:
                output.put((None,) * 5)
                logging.log_exception(
                    (
                        f"Failed to render frame {image.tell()} of {image._source!r}"
                        + (f" during loop {animator.loop_no}" if repeat != -1 else "")
                    ),
                    logger,
                )
                notify.notify(str(e), level=notify.ERROR)
                block = True
        else:
            if not data:
                break

            if data is ...:
                try:
                    animator.close()
                except AttributeError:  # First time
                    pass
                clear_queue(output)
                ready.set()
                block = True
            elif isinstance(data, tuple):
                new_repeat, frame_no = data
                animator = ImageIterator(
                    image, new_repeat, f"1.1{alpha}{style_spec}", cached
                )
                next(animator)
                animator.seek(frame_no)
                image.set_size(Size.AUTO, maxsize=size)
                block = False
            else:
                # A new image is always created to ensure:
                # 1. the seek position of the image
                #    in MainProcess::MainThread is always correct, since frames should
                #    be rendered ahead.
                # 2. the image size is not changed from another thread in the course of
                #    animation (could occur when an animated image is opened from a
                #    grid wherein its cell is yet to be rendered, since GridRenderer
                #    will continue rendering cells alongside the animation).
                image = ImageClass.from_file(data)
                animator = ImageIterator(
                    image, repeat, f"1.1{alpha}{style_spec}", cached
                )
                image.set_size(Size.AUTO, maxsize=size)
                block = False

    clear_queue(output)


def render_grid_images(
    input: Queue | mp_Queue,
    output: Queue | mp_Queue,
    ImageClass: type,
    alpha: str,
    style_spec: str,
):
    """Renders images for the grid.

    Intended to be executed in a subprocess or thread.
    """
    while True:
        source, thumbnail, canvas_size = input.get()

        if not source:  # Quitting
            break

        # Using `BaseImage` for padding will use more memory since all the
        # spaces will be in the render output string, and theoretically more time
        # with all the checks and string splitting & joining.
        # While `ImageCanvas` is better since it only stores the main image render
        # string (as a list though) then generates and yields the complete lines
        # **as needed**. Trimmed padding lines are never generated at all.
        try:
            image = ImageClass.from_file(thumbnail or source)
            image.set_size(Size.FIT if thumbnail else Size.AUTO, maxsize=canvas_size)
            output.put(
                (
                    source,
                    thumbnail,
                    f"{image:1.1{alpha}{style_spec}}",
                    canvas_size,
                    image.rendered_size,
                )
            )
        except Exception:
            output.put((source, thumbnail, None, canvas_size, None))

    clear_queue(output)


def render_images(
    input: Queue | mp_Queue,
    output: Queue | mp_Queue,
    ImageClass: type,
    style_spec: str,
):
    """Renders images.

    Intended to be executed in a subprocess or thread.
    """
    while True:
        source, size, alpha, faulty = input.get()

        if not source:  # Quitting
            break

        # Using `BaseImage` for padding will use more memory since all the
        # spaces will be in the render output string, and theoretically more time
        # with all the checks and string splitting & joining.
        # While `ImageCanvas` is better since it only stores the main image render
        # string (as a list though) then generates and yields the complete lines
        # **as needed**. Trimmed padding lines are never generated at all.
        try:
            image = ImageClass.from_file(source)
            image.set_size(Size.AUTO, maxsize=size)
            output.put((f"{image:1.1{alpha}{style_spec}}", image.rendered_size))
        except Exception as e:
            output.put((None, None))
            # `faulty` ensures a fault is logged only once per `Image` instance
            if not faulty:
                logging.log_exception(f"Failed to load or render {source!r}", logger)
            notify.notify(str(e), level=notify.ERROR)

    clear_queue(output)


logger = _logging.getLogger(__name__)
anim_render_queue = Queue()
grid_render_queue = Queue()
grid_thumbnail_queue = Queue()
image_render_queue = Queue()
grid_renderer_in_sync = Event()
grid_thumbnailer_in_sync = Event()
thumbnail_render_lock = Lock()
thumbnail_sources: dict[str, tuple[str]] = {}
# Main thumbnail cache
thumbnail_cache: dict[str, str] = {}
# For evicted and deduplicated thumbnails still being rendered
extra_thumbnail_cache: dict[str, str] = {}
# Each value contains the sources currently using the thumbnail
thumbnails_being_rendered: defaultdict[str, set] = defaultdict(set)

# `GridRenderManager` is actually "in sync" initially.
#
# Removing these may result in ocassional deadlocks because at init, the thread
# may detect "out of sync" and try to prep for a new grid but **without a grid
# delimiter**.
# The deadlock is unpredictable and timing-dependent, as it only happens when
# `main.display_images()` signals "out of sync" **after** the thread has responded to
# the false initial "out of sync". Hence, the thread blocks on trying to get a grid
# delimiter.
# When another "out of sync" is signaled (with a grid delimiter), the thread takes
# the delimiter as for the previous "out of sync" and comes back around to block
# again since the event will be unset after consuming the delimiter.
grid_renderer_in_sync.set()
grid_thumbnailer_in_sync.set()

# Updated from `.tui.init()`
anim_style_specs = {"kitty": "+W", "iterm2": "+Wm1"}
grid_style_specs = {"kitty": "+L", "iterm2": "+L"}
image_style_specs = {"kitty": "+W", "iterm2": "+W"}

# Set from `.tui.init()`
# # Corresponding to command-line args and/or config options
ANIM_CACHED: bool | int
FRAME_DURATION: float
REPEAT: int
THUMBNAIL_CACHE_SIZE: int

import os
import sys
import yaml
import json
import pyseq
import shlex
import argparse
import subprocess

from pathlib import Path

from .encoders import encoder_factory

try:
    from yaml import CSafeLoader as SafeLoader
    from yaml import CSafeDumper as SafeDumper

except ImportError:
    from yaml import SafeLoader, SafeDumper

import opentimelineio as otio

# Test config files
from .utils import (
    create_clip,
    get_media_info,
    get_source_path,
    get_nearest_model,
    get_test_metadata_dict,
    get_source_metadata_dict
)

ENCODE_TEST_SUFFIX = '.yml'
SOURCE_SUFFIX = '.yml'

# We assume macos and linux both have the same binary name
FFMPEG_BIN = os.getenv(
    'FFMPEG_BIN',
    'win' in sys.platform and 'ffmpeg.exe' or 'ffmpeg'
)


VMAF_LIB_DIR = os.getenv(
    'VMAF_LIB_DIR',
    f'{os.path.dirname(__file__)}/../.venv/usr/local/lib/x86_64-linux-gnu'
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--source-folder',
        action='store',
        default='./sources',
        help='Where to look for source media files'
    )

    parser.add_argument(
        '--test-config-dir',
        action='store',
        default='./test_configs',
        help='Where to look for *.yml files containing test descriptions'
    )

    parser.add_argument(
        '--test-config',
        action='store',
        dest='test_config_file',
        default=None,
        help='Specify a single test config file to run'
    )

    parser.add_argument(
        '--prep-sources',
        action='store_true',
        default=False,
        help='Create *.yml files from media in --source-folder used as sources '
             'in encoding tests'
    )

    parser.add_argument(
        '--encoded-folder',
        action='store',
        default='./encoded',
        help='Where to store the encoded files'
    )

    parser.add_argument(
        '--encode-all',
        action='store_true',
        default=False,
        help=argparse.SUPPRESS
        # help='Encode all tests. Default to only encoding new tests'
    )

    parser.add_argument(
        '--output',
        action='store',
        default='encoding-test-results.otio',
        help='Path to results file including ".otio" extenstion '
             '(default: ./encoding-test-results.otio)'
    )

    args = parser.parse_args()

    if not args.output.endswith('.otio'):
        args.output += '.otio'

    return args


def parse_config_file(path):
    config_file = path.as_posix()
    with open(config_file, 'rt') as f:
        config = list(yaml.load_all(f, SafeLoader))

    test_configs = []
    for test_config in config:
        test_configs.append(test_config)

    return test_configs


def create_config_from_source(path, startframe=None):
    config_data = {'images': False}

    media_info = get_media_info(path, startframe)
    if not media_info:
        return

    config_data.update(media_info)

    if startframe:
        config_data['images'] = True

    config_path = path.with_suffix(path.suffix + SOURCE_SUFFIX)
    with open(config_path, 'wt') as f:
        yaml.dump(config_data, f, SafeDumper, indent=4, sort_keys=False)
        print(f'Successfully wrote source file: "{config_path}"')


def scantree(args, path, suffix=None):
    """Recursively yield DirEntry objects for given directory."""
    for entry in os.scandir(path):
        if entry.is_dir(follow_symlinks=False):
            sequences = pyseq.get_sequences(entry.path)
            if args.prep_sources and sequences:
                for sequence in sequences:
                    if sequence.name.endswith(SOURCE_SUFFIX):
                        # Can ignore any .source files.
                        continue
                    if sequence.length() < 2:
                        yield from scantree(args, entry.path, suffix)
                    else:
                        yield sequence
            else:
                yield from scantree(args, entry.path, suffix)

        else:
            if suffix and not entry.path.endswith(suffix):
                continue

            yield entry


def create_source_config_files(args):
    """ Create source config files based on ffprobe results. """

    # for item in os.scandir(root):
    for item in scantree(args, args.source_folder):
        startframe = None
        if isinstance(item, pyseq.Sequence):
            startframe = item.start()
            pad = f'%0{len(max(item.digits, key=len))}d'
            path = Path(item.format('%D%h') + pad + item.format('%t'))

        else:
            path = Path(str(item.path))

        if path.suffix == SOURCE_SUFFIX:
            # We only register new media
            continue

        create_config_from_source(path, startframe=startframe)

    print(
        f'Done creating source files. '
        f'Make sure to do adjustments of in point, duration and so on.'
    )


def get_configs(args, root_path, config_type):
    configs = []
    for item in scantree(args, root_path, suffix=config_type):
        path = Path(item.path)
        if path.suffix == config_type:
            configs.extend(parse_config_file(path))

    return configs


def tests_only(test_configs):
    for config in test_configs:
        for section in config:
            if section.lower().startswith('test'):
                yield config[section]


def vmaf_compare(source_clip, test_ref, testname):
    vmaf_cmd = '\
{ffmpeg_bin} \
{reference} \
-i "{distorted}" \
-vframes {duration} \
-lavfi \
\"[0:v]setpts=PTS-STARTPTS[reference]; \
[1:v]setpts=PTS-STARTPTS[distorted]; \
[distorted][reference]\
libvmaf=log_fmt=json:\
log_path=compare_log.json:\
feature="name=psnr":\
model=path={vmaf_model}\" \
-f null -\
'
    # Get settings from metadata used as basis for encoded media
    source_meta = get_source_metadata_dict(source_clip)
    input_args = ''
    if source_meta.get('images'):
        input_args = f"-start_number {source_meta.get('in')}"

    source_path, _ = get_source_path(source_clip)
    reference = f'{input_args} -i "{source_path}" '

    # Assuming all encoded files are video files for now
    distorted = test_ref.target_url

    cmd = vmaf_cmd.format(
        ffmpeg_bin=FFMPEG_BIN,
        reference=reference,
        distorted=distorted,
        duration=source_meta.get('duration'),
        vmaf_model=get_nearest_model(int(source_meta.get('width', 1920)))
    )
    print('VMAF command:', cmd)

    env = os.environ
    if 'LD_LIBRARY_PATH' in env:
        env['LD_LIBRARY_PATH'] += f'{os.pathsep}{VMAF_LIB_DIR}'

    else:
        env.update({'LD_LIBRARY_PATH': VMAF_LIB_DIR})

    subprocess.call(shlex.split(cmd), env=env)
    with open(f'compare_log.json', 'rb') as f:
        raw_results = json.load(f)

    results = {
        'vmaf': raw_results['pooled_metrics'].get('vmaf'),
        'psnr': raw_results['pooled_metrics'].get('psnr')
    }

    enc_meta = get_test_metadata_dict(test_ref, testname)
    enc_meta['results'] = results


def prep_sources(args, track):
    source_configs = get_configs(args, args.source_folder, SOURCE_SUFFIX)
    for config in source_configs:
        source_clip = create_clip(config)
        track.append(source_clip)


def run_tests(args, test_configs, track):
    for source_clip in track:
        references = source_clip.media_references()

        for test_config in tests_only(test_configs):
            # perform encoding test
            encoder = encoder_factory(
                source_clip,
                test_config,
                Path(args.encoded_folder)
            )
            results = encoder.run_wedges()
            for test_name, test_ref in results.items():
                vmaf_compare(source_clip, test_ref, test_name)

            references.update(results)

        # Add media references to clip
        source_clip.set_media_references(
            references, source_clip.DEFAULT_MEDIA_KEY
        )


def main():
    args = parse_args()

    # Make sure we have a folder for test configs
    Path(args.test_config_dir).mkdir(parents=True, exist_ok=True)

    if args.prep_sources:
        create_source_config_files(args)
        return

    # Make sure we have a destination folder
    Path(args.encoded_folder).mkdir(parents=True, exist_ok=True)

    # Load test config file(s)
    test_configs = []
    if args.test_config_file:
        test_configs.extend(parse_config_file(Path(args.test_config_file)))

    else:
        test_configs.extend(
            get_configs(args, args.test_config_dir, ENCODE_TEST_SUFFIX)
        )

    # Create a track to hold clips
    track = otio.schema.Track(name='aswf_enctests')

    # Prep source files
    prep_sources(args, track)

    # Run tests
    run_tests(args, test_configs, track)

    # Store results in a timeline, so we can view the results in otioview
    timeline = otio.schema.Timeline()
    timeline.tracks.append(track)

    # Serialize to *.otio
    otio.adapters.write_to_file(timeline, args.output)


if __name__== '__main__':
    main()
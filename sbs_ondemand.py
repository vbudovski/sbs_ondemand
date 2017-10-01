#!/usr/bin/env python

# Copyright 2017 Vitaly Budovski
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import argparse
import json
import logging
import multiprocessing
import os
import signal
import sqlite3
import sys
import time
from abc import ABC
from io import BytesIO

import ffmpeg
import m3u8
from jsonfinder import jsonfinder
from lxml import etree, html

import pycurl

logging.basicConfig(level=logging.INFO)

API_ROOT = 'http://www.sbs.com.au/api/'
MAX_RETRIES = 3


def get_with_retry(url):
    retries_left = MAX_RETRIES

    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.FOLLOWLOCATION, True)

    while retries_left > 0:
        buffer = BytesIO()
        c.setopt(c.WRITEDATA, buffer)
        c.perform()

        response_code = c.getinfo(c.RESPONSE_CODE)
        if response_code == 200:
            c.close()
            return buffer.getvalue().decode()

        retries_left -= 1
        time.sleep(1)

    c.close()
    return None


class SBSOnDemandAsset(ABC):
    _id = None
    _title = None

    def id(self):
        return self._id

    def title(self):
        return self._title


class SBSOnDemandTVProgram(SBSOnDemandAsset):
    _seasons = None
    _episodes = None

    def __init__(self, data):
        super().__init__()

        self._title = data['name']
        self._id = data['id']

        r = get_with_retry('{}video_program?context=web2&id={}'.format(API_ROOT, self._id))
        if r is None:
            logging.error('Failed to fetch data for {}'.format(self.title()))
            raise RuntimeError('Error fetching program data')

        episode_data = json.loads(r)['program']

        if data['type'] == 'program_series':
            self._seasons = len(episode_data['seasons'])

            url = episode_data['url']
        else:
            url = '{}video_feed/f/Bgtm9B/sbs-section-programs/?byCustomValue={{pilatId}}{{{}}}'.format(
                API_ROOT,
                episode_data['pl1$pilatId'],
            )

        r = get_with_retry(url)

        self._episodes = []
        for episode in json.loads(r)['entries']:
            video_id = episode['id'][episode['id'].rfind('/') + 1:]

            self._episodes.append({
                'title': episode['title'],
                'id': video_id,
            })

    def episodes(self):
        return self._episodes


class SBSOnDemandMovie(SBSOnDemandAsset):
    def __init__(self, data):
        super().__init__()

        self._title = data['title']
        self._id = data['id'][data['id'].rfind('/') + 1:]


def sigint_handler(sig, frame):
    logging.warning('Terminating...')
    sys.exit(1)


class SBSOnDemand(object):
    MAX_RESULTS = 10

    _connection = None

    def __enter__(self):
        self._connection = sqlite3.connect('sbs_ondemand.db')
        self.create_tables()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._connection.close()

    def create_tables(self):
        cursor = self._connection.cursor()

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS sbs_titles (
                title_id integer PRIMARY KEY,
                title text NOT NULL
            );
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS sbs_tv_episodes (
                episode_id integer PRIMARY KEY,
                title text NOT NULL,
                title_id integer NOT NULL,
                FOREIGN KEY (title_id) REFERENCES sbs_titles(title_id)
            );
            '''
        )

        self._connection.commit()

    @staticmethod
    def movie_list():
        # Do an initial fetch to find total number of results.
        url = '{}video_feed/f/Bgtm9B/sbs-section-programs/?form=json&count=true&range=1-{}&'\
            'byCategories=Section%2FPrograms,Film,Film,!Film%2FShort%20Film'
        r = get_with_retry(url.format(API_ROOT, 1))

        # Fetch all results in one page.
        total_results = json.loads(r)['totalResults']
        r = get_with_retry(url.format(API_ROOT, total_results))

        return json.loads(r)['entries']

    @staticmethod
    def program_list():
        r = get_with_retry('{}video_programs/all?upcoming=1'.format(API_ROOT))
        return json.loads(r)['entries']

    def synchronise(self):
        cursor = self._connection.cursor()

        for movie in self.movie_list():
            logging.info('Fetching data for {}'.format(movie['title']))
            m = SBSOnDemandMovie(movie)

            cursor.execute('INSERT OR IGNORE INTO sbs_titles VALUES(?, ?)', (m.id(), m.title()))

        self._connection.commit()

        for program in self.program_list():
            logging.info('Fetching data for {}'.format(program['name']))
            try:
                p = SBSOnDemandTVProgram(program)
            except RuntimeError:
                continue

            cursor = self._connection.cursor()

            cursor.execute('INSERT OR IGNORE INTO sbs_titles VALUES(?, ?)', (p.id(), p.title()))
            for episode in p.episodes():
                cursor.execute(
                    'INSERT OR IGNORE INTO sbs_tv_episodes VALUES(?, ?, ?)',
                    (episode['id'], episode['title'], p.id())
                )

            self._connection.commit()

    @staticmethod
    def save_video(url, output_dir):
        smil_url = get_with_retry('http:{}'.format(url))
        smil_tree = etree.fromstring(smil_url)
        namespace = {'smil': 'http://www.w3.org/2005/SMIL21/Language'}

        video_tree = smil_tree.xpath(
            '//smil:body/smil:seq/smil:par/smil:video|//smil:body/smil:seq/smil:video',
            namespaces=namespace,
        )
        srt_tree = smil_tree.xpath(
            '//smil:body/smil:seq/smil:par/smil:textstream[@type="text/srt"]',
            namespaces=namespace,
        )

        title = video_tree[0].attrib['title']

        if srt_tree:
            # Subtitles found.
            srt_url = srt_tree[0].attrib['src']

            output_path = os.path.join(output_dir, '{}.srt'.format(title))
            with open(output_path, 'w') as f:
                srt_content = get_with_retry(srt_url)
                f.write(srt_content)

        video_url = video_tree[0].attrib['src']

        m3u8_obj = m3u8.load(video_url)
        best_quality = max(m3u8_obj.playlists, key=lambda p: p.stream_info.bandwidth)
        stream_uri = best_quality.uri

        output_path = os.path.join(output_dir, '{}.mp4'.format(title))
        ffmpeg.input(stream_uri).output(output_path, codec='copy', loglevel='warning').run()

    @staticmethod
    def fetch_video_url(video_id, file_number, title, total, output_dir):
        logging.info('Downloading {} of {}: {}'.format(file_number, total, title))

        r = get_with_retry('https://www.sbs.com.au/ondemand/video/single/{}?context=web'.format(video_id))
        tree = html.fromstring(r)

        script_tags = tree.xpath('//head/script')

        for tag in script_tags:
            if tag.text:
                for _, _, obj in jsonfinder(tag.text):
                    if obj and 'playerURL' in obj:
                        SBSOnDemand.save_video(obj['releaseUrls']['htmldesktop'], output_dir=output_dir)
                        break

    @staticmethod
    def _fetch_video_url_wrapper(kwargs):
        SBSOnDemand.fetch_video_url(**kwargs)

    def download(self, title, output_dir, download_threads):
        cursor = self._connection.cursor()

        cursor.execute(
            "SELECT * from sbs_titles WHERE LOWER(title) LIKE ? LIMIT ?",
            ('%{}%'.format(title.lower()), SBSOnDemand.MAX_RESULTS + 1)
        )

        results = list(cursor.fetchall())

        if len(results) == 0:
            logging.info('No results')
            return

        if len(results) > 1:
            message = 'Multiple titles found:\n\n'
            message += '\n'.join(title for _, title in results[:SBSOnDemand.MAX_RESULTS])
            if len(results) > SBSOnDemand.MAX_RESULTS:
                message += '\n...'

            logging.info(message)
            return

        title_id, video_title = results[0]

        cursor.execute('SELECT episode_id, title FROM sbs_tv_episodes WHERE title_id = ?', (title_id,))

        episodes = list(cursor.fetchall())
        if not episodes:
            # Download the movie.
            SBSOnDemand.fetch_video_url(title_id, 1, video_title, 1)
            return

        # This is a TV series, so download the episodes.
        total_episodes = len(episodes)
        args = []
        for index, (episode_id, episode_title) in enumerate(episodes):
            kwargs = {
                'video_id': episode_id,
                'file_number': index + 1,
                'title': episode_title,
                'total': total_episodes,
                'output_dir': output_dir,
            }
            args.append(kwargs)

        pool = multiprocessing.Pool(processes=download_threads)
        pool.map(SBSOnDemand._fetch_video_url_wrapper, args)


def main():
    DEFAULT_DOWNLOAD_THREADS_COUNT = 5

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='subparser_name')

    subparsers.add_parser('sync')

    download_parser = subparsers.add_parser('download')
    download_parser.add_argument('title', help='All or part of a movie or tv series title')
    download_parser.add_argument(
        'output_dir',
        metavar='output-dir',
        help='The directory in which to save the downloads',
    )
    download_parser.add_argument(
        '-n',
        '--download-threads',
        type=int,
        default=DEFAULT_DOWNLOAD_THREADS_COUNT,
        dest='download_threads',
        help='The number of files to download concurrently. Default is {}.'.format(DEFAULT_DOWNLOAD_THREADS_COUNT),
    )

    args = parser.parse_args()

    with SBSOnDemand() as sbs:
        if args.subparser_name == 'sync':
            sbs.synchronise()
        elif args.subparser_name == 'download':
            output_dir = os.path.realpath(args.output_dir)
            if not os.path.isdir(output_dir):
                logging.error('Invalid output directory specified')
                sys.exit(1)

            sbs.download(args.title, output_dir, args.download_threads)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, sigint_handler)
    main()

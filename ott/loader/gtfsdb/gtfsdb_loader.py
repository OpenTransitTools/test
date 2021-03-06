from ott.utils import gtfs_utils
from ott.utils import db_utils
from ott.utils import exe_utils
from ott.utils import file_utils

from ott.utils.parse.cmdline import gtfs_cmdline
from ott.utils.cache_base import CacheBase
from ott.loader.gtfs.gtfs_cache import GtfsCache
from ott.loader.gtfsdb_realtime.gtfsdb_realtime_loader import GtfsdbRealtimeLoader

from gtfsdb.api import database_load
from gtfsdb import scripts

import os
import logging
log = logging.getLogger(__file__)


class GtfsdbLoader(CacheBase):
    """
    load GTFS data into a gtfsdb
    """
    feeds = []
    db_url = None
    is_geospatial = False
    current_tables = True
    err_ext = "-error_loading"

    def __init__(self, feed_filter="all"):
        super(GtfsdbLoader, self).__init__(section='gtfs')
        self.feeds = gtfs_utils.get_feeds_from_config(self.config, feed_filter)
        self.db_url = self.config.get('url', section='db', def_val='postgresql+psycopg2://ott@127.0.0.1:5432/ott')
        self.is_geospatial = self.config.get_bool('is_geospatial', section='db')
        self.current_tables = self.config.get_bool('current_tables', section='db', def_val=True)

    @classmethod
    def get_feed_name(cls, feed):
        db_schema_name = gtfs_utils.get_schema_name_from_feed(feed)
        return db_schema_name

    def get_feed_path(self, feed):
        feed_path = os.path.join(self.cache_dir, feed['name'])
        return feed_path

    def get_dump_path(self, feed_name):
        return "{}/{}.tar".format(self.cache_dir, feed_name)

    def get_gtfsdb_args(self, feed, current_tables=False):
        """
        get the Database(kwargs) for gtfsdb from the feed

        :param feed: see app.ini [gtfs] section
        :return: hash table of args for gtfsdb
        """
        feed_name = self.get_feed_name(feed)

        kwargs = {}
        kwargs['url'] = self.db_url
        kwargs['current_tables'] = self.current_tables or current_tables
        if "sqlite:" not in self.db_url:
            kwargs['is_geospatial'] = self.is_geospatial
            kwargs['schema'] = feed_name
        return kwargs

    def load_feed(self, feed):
        """
        insert a GTFS feed into configured db
        """
        ret_val = True

        # step 1: get cached feed path and feed name (see 'feeds' in config/app.ini)
        feed_path = self.get_feed_path(feed)
        feed_name = self.get_feed_name(feed)

        # step 2: make args for gtfsdb
        kwargs = self.get_gtfsdb_args(feed)

        # step 3: load this feed into gtfsdb
        log.info("loading {} ({}) into gtfsdb {}".format(feed_name, feed_path, self.db_url))
        try:
            database_load(feed_path, **kwargs)
        except Exception as e:
            ret_val = False
            file_utils.mv(feed_path, feed_path + self.err_ext)
            log.error("DATABASE ERROR : {}".format(e))
        return ret_val

    def check_db(self, force_update=False):
        """
        check the local cache of GTFS feeds, and decide whether we should reload a given feed based on feed info
        """
        # import pdb; pdb.set_trace()
        export_list = []

        # step 1: loop thru all our feeds
        purged = False
        for f in self.feeds:
            reload = False

            # step 2: see if the GTFS cache has a newer feed than what we have in this GTFS-DB cache
            if GtfsCache.compare_feed_against_cache(f, self.cache_dir, force_update):
                reload = True

            # step 3: okay, reload this GTFS feed into the database
            if reload or force_update:
                # step 3a: we should purge any GTFS-error files that might have been generated on the last load
                #          NOTE: we'll also check the database on this step ... make sure it's available and ready
                if not purged:
                    db_utils.check_create_db(self.db_url, self.is_geospatial)
                    file_utils.purge(self.cache_dir, ".*" + self.err_ext)
                    purged = True

                # step 3b: load the feed into the database
                self.load_feed(f)
                export_list.append(f)

        if len(export_list) > 0:
            from .gtfsdb_exporter import GtfsdbExporter
            GtfsdbExporter.dump(export_list, clean_db=True)

    def restore_feed(self, feed, bkup="-processed"):
        """
        run the postgres db restore
        first tho, move any old schemas out of the way as <schema>_old
        (otherwise, there will be errors and the db won't load correctly)
        """
        ret_val = True
        feed_name = ""
        try:
            feed_name = self.get_feed_name(feed)
            dump_path = self.get_dump_path(feed_name)
            if file_utils.exists(dump_path):
                # step a: remove <schema>_OLD
                rm_schema_exe = self.config.get('rm_schema', section='db').format(schema=feed_name)
                log.info(rm_schema_exe)
                exe_utils.run_cmd(rm_schema_exe, shell=True)

                # step b: move existing <schema> to <schema>_OLD
                mv_schema_exe = self.config.get('mv_schema', section='db').format(schema=feed_name)
                log.info(mv_schema_exe)
                exe_utils.run_cmd(mv_schema_exe, shell=True)

                # step c: restore new data
                restore_exe = self.config.get('restore', section='db').format(schema=feed_name, dump_file=dump_path)
                log.info(restore_exe)
                exe_utils.run_cmd(restore_exe, shell=True)

                # step d:
                file_utils.mv(dump_path, dump_path + bkup)
            else:
                log.info("{} doesn't exist, so won't try to pg_restore".format(dump_path))
        except Exception as e:
            ret_val = False
            log.error("DB RESTORE ERROR {} : {}".format(feed_name, e))
        return ret_val

    @classmethod
    def restore(cls):
        """ run pg_restore on any existing pg_dump cache/*.tar files """

        # step 1: restore the gtfsdb feed
        create_rt_tables = False
        db = GtfsdbLoader()
        for f in db.feeds:
            new_load = db.restore_feed(f)
            if new_load:
                create_rt_tables = True

        # step 2: re-create the RT tables on new db refresh
        if create_rt_tables:
            rt = GtfsdbRealtimeLoader(db.db_url)
            rt.load_all(is_geospatial=db.is_geospatial, create_db=True)

    @classmethod
    def load(cls):
        """
        run the gtfsdb loader against all the specified feeds from config/app.ini
        NOTE: this is effectively a main method for downloading, caching and db loading new/updated gtfs feeds
        """
        args = gtfs_cmdline.gtfs_parser('bin/gtfsdb_load')
        db = GtfsdbLoader(args.agency_id)
        db.check_db(force_update=args.force)

    @classmethod
    def current_load(cls):
        """
        run the gtfsdb current table loader against all the specified feeds from config/app.ini
        :usage; this method should be called weekly (or daily) to move data into the current tables to keep them fresh
        :note:  this is effectively a main method for populating the current tables.
        """
        #import pdb; pdb.set_trace()
        args = gtfs_cmdline.gtfs_parser('bin/gtfsdb_current_load')
        db = GtfsdbLoader(args.agency_id)
        for f in db.feeds:
            kwargs = db.get_gtfsdb_args(f, current_tables=True)
            scripts.current_tables_load(**kwargs)

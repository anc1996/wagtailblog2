#!/usr/bin/env bash
set -e
cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2

export BLOG_INDEX_LISTING_ENGINE_V2=True
export BLOG_INDEX_CACHE_V2=True
export BLOG_SIDEBAR_CACHE_V2=True
export BLOG_ARCHIVE_PROGRESSIVE_V2=True
export BLOG_INDEX_ETAG_V2=True
export BLOG_INDEX_COMPAT_POLYMORPHIC_QUERY=True
export DJANGO_SETTINGS_MODULE=wagtailblog3.settings.dev

mkdir -p /mnt/f/openclaw/workspace/wagtail/wagtailblog2/logs

/root/anaconda3/envs/wagtailblog-test/bin/python manage.py runserver 0.0.0.0:8080 --noreload

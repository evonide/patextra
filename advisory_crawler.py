#!/usr/bin/env python3

from scrapy.selector import Selector
from scrapy.spiders import Spider
from scrapy.http import Request
from urllib.parse import urljoin
from urllib.parse import urlparse
import re

# Pidgin settings
ADVISORY_URL = 'https://pidgin.im/news/security/'
START_PAGES = [ADVISORY_URL + "?offset={}".format(i*50) for i in range(3)]
SINGLE_ADVISORY_REGEX = r'.*id=(\d*)'
COMMIT_REGEX = r'.*bitbucket.*/commits/(.*)'


# Mozilla settings
"""
ADVISORY_URL = "https://www.mozilla.org/en-US/security/advisories/"
START_PAGES = [ADVISORY_URL]
SINGLE_ADVISORY_REGEX = r'.*mfsa\d*\-\d*.*'
COMMIT_REGEX = r'.*bugzilla.*\?id=(\d*)'
"""

class AdvisorySpider(Spider):
    name = 'advisorySpider'
    custom_settings = {
        'LOG_ENABLED': False,
        'USER_AGENT': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.10; rv:39.0) Gecko/20100101 Firefox/39.0'
    }

    def start_requests(self):
        self.allowedDomains = []
        self.allowedDomains.append(urlparse(ADVISORY_URL).hostname)

        for start_page in START_PAGES:
            yield Request(start_page, callback=self.parse)

    def parse(self, response):
        advisory_regex = re.compile(SINGLE_ADVISORY_REGEX)

        hxs = Selector(response)
        for url in hxs.xpath('//a/@href').extract():
            absolute_url = urljoin(response.url, url)
            commit_regex = re.compile(COMMIT_REGEX)
            matches_commit = re.match(commit_regex, absolute_url)
            if matches_commit:
                commit_id = matches_commit.group(1)
                #matched_bug_id = advisory_regex.match(response.url)
                #bug_id = matched_bug_id.group(1)
                #print(bug_id + "\t" + )
                print(commit_id)

            if advisory_regex.match(absolute_url):
                yield Request(absolute_url, callback=self.parse)
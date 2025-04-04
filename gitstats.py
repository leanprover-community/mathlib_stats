#!/usr/bin/env python3
"""
Copyright (c) 2007-2014 Heikki Hokkanen <hoxu@users.sf.net> & others (see doc/AUTHOR)
"""
import datetime
import getopt
import glob
import os
import pickle
import platform
import re
import shutil
import subprocess
import sys
import time
import zlib
from multiprocessing import Pool
from pathlib import Path
import json

os.environ['LC_ALL'] = 'C'

GNUPLOT_COMMON = 'set terminal png transparent size 640,240\nset size 1.0,1.0\n'
ON_LINUX = (platform.system() == 'Linux')
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

exectime_internal = 0.0
exectime_external = 0.0
time_start = time.time()


conf = {
        'max_domains': 10,
        'max_ext_length': 10,
        'style': 'gitstats.css',
        'max_authors': 15,
        'authors_top': 5,
        'commit_begin': '',
        'commit_end': 'HEAD',
        'linear_linestats': 1,
        'project_name': '',
        'processes': 8,
        'start_date': ''
}

def getpipeoutput(cmds, quiet = False):
        global exectime_external
        start = time.time()
        if not quiet and ON_LINUX and os.isatty(1):
                print('>> ' + ' | '.join(cmds), end=' ')
                sys.stdout.flush()
        p = subprocess.Popen(cmds[0], stdout = subprocess.PIPE, shell = True)
        processes=[p]
        for x in cmds[1:]:
                p = subprocess.Popen(x, stdin = p.stdout, stdout = subprocess.PIPE, shell = True)
                processes.append(p)
        output = p.communicate()[0]
        for p in processes:
                p.wait()
        end = time.time()
        if not quiet:
                if ON_LINUX and os.isatty(1):
                        print('\r', end=' ')
                print('[%.5f] >> %s' % (end - start, ' | '.join(cmds)))
        exectime_external += (end - start)
        return output.decode().rstrip('\n')

def getlogrange(defaultrange = 'HEAD', end_only = True):
        commit_range = getcommitrange(defaultrange, end_only)
        if len(conf['start_date']) > 0:
                return '--since="%s" "%s"' % (conf['start_date'], commit_range)
        return commit_range

def getcommitrange(defaultrange = 'HEAD', end_only = False):
        if len(conf['commit_end']) > 0:
                if end_only or len(conf['commit_begin']) == 0:
                        return conf['commit_end']
                return '%s..%s' % (conf['commit_begin'], conf['commit_end'])
        return defaultrange

def getkeyssortedbyvalues(dict):
        return [el[1] for el in sorted([(el[1], el[0]) for el in list(dict.items())])]

# dict['author'] = { 'commits': 512 } - ...key(dict, 'commits')
def getkeyssortedbyvaluekey(d, key):
        return [el[1] for el in sorted([(d[el][key], el) for el in list(d.keys())])]

def getstatsummarycounts(line):
        numbers = re.findall(r'\d+', line)
        if   len(numbers) == 1:
                # neither insertions nor deletions: may probably only happen for "0 files changed"
                numbers.append(0)
                numbers.append(0)
        elif len(numbers) == 2 and line.find('(+)') != -1:
                numbers.append(0)    # only insertions were printed on line
        elif len(numbers) == 2 and line.find('(-)') != -1:
                numbers.insert(1, 0) # only deletions were printed on line
        return numbers

VERSION = 0
def getversion():
        global VERSION
        if VERSION == 0:
                gitstats_repo = os.path.dirname(os.path.abspath(__file__))
                VERSION = getpipeoutput(["git --git-dir=%s/.git --work-tree=%s rev-parse --short %s" %
                        (gitstats_repo, gitstats_repo, getcommitrange('HEAD').split('\n')[0])])
        return VERSION

def getgitversion():
        return getpipeoutput(['git --version']).split('\n')[0]


def getnumoffilesfromrev(time_rev):
        """
        Get number of files changed in commit
        """
        time, rev = time_rev
        return (int(time), rev, int(getpipeoutput(['git ls-tree -r --name-only "%s"' % rev, 'wc -l']).split('\n')[0]))

def getnumoflinesinblob(ext_blob):
        """
        Get number of lines in blob
        """
        ext, blob_id = ext_blob
        return (ext, blob_id, int(getpipeoutput(['git cat-file blob %s' % blob_id, 'wc -l']).split()[0]))

class DataCollector:
        """Manages data collection from a revision control repository."""
        def __init__(self):
                self.stamp_created = time.time()
                self.cache = {}
                self.total_authors = 0
                self.activity_by_hour_of_day = {} # hour -> commits
                self.activity_by_day_of_week = {} # day -> commits
                self.activity_by_month_of_year = {} # month [1-12] -> commits
                self.activity_by_hour_of_week = {} # weekday -> hour -> commits
                self.activity_by_hour_of_day_busiest = 0
                self.activity_by_hour_of_week_busiest = 0
                self.activity_by_year_week = {} # yy_wNN -> commits
                self.activity_by_year_week_peak = 0

                self.authors = {} # name -> {commits, first_commit_stamp, last_commit_stamp, last_active_day, active_days, lines_added, lines_removed}

                self.total_commits = 0
                self.total_files = 0
                self.authors_by_commits = 0

                # domains
                self.domains = {} # domain -> commits

                # author of the month
                self.author_of_month = {} # month -> author -> commits
                self.author_of_year = {} # year -> author -> commits
                self.commits_by_month = {} # month -> commits
                self.commits_by_year = {} # year -> commits
                self.lines_added_by_month = {} # month -> lines added
                self.lines_added_by_year = {} # year -> lines added
                self.lines_removed_by_month = {} # month -> lines removed
                self.lines_removed_by_year = {} # year -> lines removed
                self.first_commit_stamp = 0
                self.last_commit_stamp = 0
                self.last_active_day = None
                self.active_days = set()

                # the commit label is the first regex word in the commit subject, later normalized to lower case
                self.commit_label_re = re.compile(r'^\W*(?P<label>\w+)')
                self.commits_by_type_by_month = {} # month -> commit type -> commits
                self.commits_by_type_by_year = {} # year -> commit type -> commits
                self.commits_by_label = {} # commit type -> commits

                # lines
                self.total_lines = 0
                self.total_lines_added = 0
                self.total_lines_removed = 0

                # size
                self.total_size = 0

                # timezone
                self.commits_by_timezone = {} # timezone -> commits

                # tags
                self.tags = {}

                self.files_by_stamp = {} # stamp -> files

                # extensions
                self.extensions = {} # extension -> files, lines

                # line statistics
                self.changes_by_date = {} # stamp -> { files, ins, del }

        ##
        # This should be the main function to extract data from the repository.
        def collect(self, dir):
                self.dir = dir
                if len(conf['project_name']) == 0:
                        self.projectname = os.path.basename(os.path.abspath(dir))
                else:
                        self.projectname = conf['project_name']

        ##
        # Load cacheable data
        def loadCache(self, cachefile):
                if not os.path.exists(cachefile):
                        return
                print('Loading cache...')
                f = open(cachefile, 'rb')
                try:
                        self.cache = pickle.loads(zlib.decompress(f.read()))
                except:
                        # temporary hack to upgrade non-compressed caches
                        f.seek(0)
                        self.cache = pickle.load(f)
                f.close()

        ##
        # Produce any additional statistics from the extracted data.
        def refine(self):
                pass

        ##
        # : get a dictionary of author
        def getAuthorInfo(self, author):
                return None

        def getActivityByDayOfWeek(self):
                return {}

        def getActivityByHourOfDay(self):
                return {}

        # : get a dictionary of domains
        def getDomainInfo(self, domain):
                return None

        ##
        # Get a list of authors
        def getAuthors(self):
                return []

        def getFirstCommitDate(self):
                return datetime.datetime.now()

        def getLastCommitDate(self):
                return datetime.datetime.now()

        def getStampCreated(self):
                return self.stamp_created

        def getTags(self):
                return []

        def getTotalAuthors(self):
                return -1

        def getTotalCommits(self):
                return -1

        def getTotalFiles(self):
                return -1

        def getTotalLOC(self):
                return -1

        ##
        # Save cacheable data
        def saveCache(self, cachefile):
                print('Saving cache...')
                tempfile = cachefile + '.tmp'
                f = open(tempfile, 'wb')
                #pickle.dump(self.cache, f)
                data = zlib.compress(pickle.dumps(self.cache))
                f.write(data)
                f.close()
                try:
                        os.remove(cachefile)
                except OSError:
                        pass
                os.rename(tempfile, cachefile)

class GitDataCollector(DataCollector):
        def collect(self, dir):
                DataCollector.collect(self, dir)

                self.total_authors += int(getpipeoutput(['git shortlog -s %s' % getlogrange(), 'wc -l']))
                #self.total_lines = int(getoutput('git-ls-files -z |xargs -0 cat |wc -l'))

                # tags
                lines = getpipeoutput(['git show-ref --tags']).split('\n')
                for line in lines:
                        if len(line) == 0:
                                continue
                        (hash, tag) = line.split(' ')

                        tag = tag.replace('refs/tags/', '')
                        output = getpipeoutput(['git log "%s" --pretty=format:"%%ct %%aN" -n 1' % hash])
                        if len(output) > 0:
                                parts = output.split(' ')
                                stamp = 0
                                try:
                                        stamp = int(parts[0])
                                except ValueError:
                                        stamp = 0
                                self.tags[tag] = { 'stamp': stamp, 'hash' : hash, 'date' : datetime.datetime.fromtimestamp(stamp).strftime('%Y-%m-%d'), 'commits': 0, 'authors': {} }

                # collect info on tags, starting from latest
                tags_sorted_by_date_desc = [el[1] for el in reversed(sorted([(el[1]['date'], el[0]) for el in list(self.tags.items())]))]
                prev = None
                for tag in reversed(tags_sorted_by_date_desc):
                        cmd = 'git shortlog -s "%s"' % tag
                        if prev != None:
                                cmd += ' "^%s"' % prev
                        output = getpipeoutput([cmd])
                        if len(output) == 0:
                                continue
                        prev = tag
                        for line in output.split('\n'):
                                parts = re.split(r'\s+', line, 2)
                                commits = int(parts[1])
                                author = parts[2]
                                self.tags[tag]['commits'] += commits
                                self.tags[tag]['authors'][author] = commits

                # Collect revision statistics
                # Outputs "<stamp> <date> <time> <timezone> <author> '<' <mail> '>'"
                lines = getpipeoutput(['git rev-list --pretty=format:"%%ct %%ai %%aN <%%aE>" %s' % getlogrange('HEAD'), 'grep -v ^commit']).split('\n')
                # call getpipeoutput with git rev-list twice to isolate commit subject to sidestep any bad escaping
                # use a prefix of "s:" in case the commit subject happens to start with "commit"
                subject_lines = getpipeoutput(['git rev-list --pretty=format:"s:%%s" %s' % getlogrange('HEAD'), 'grep -v ^commit']).split('\n')
                for line, subject_line in zip(lines, subject_lines):
                        subject = subject_line[2:].lower() # everything after "s:"
                        if subject.startswith('feat: port'):
                                commit_label = 'feat: port'
                        else:
                                commit_label = self.commit_label_re.search(subject).group('label')
                        self.commits_by_label[commit_label] = self.commits_by_label.get(commit_label, 0) + 1

                        parts = line.split(' ', 4)
                        author = ''
                        try:
                                stamp = int(parts[0])
                        except ValueError:
                                stamp = 0
                        timezone = parts[3]
                        author, mail = parts[4].split('<', 1)
                        author = author.rstrip()
                        mail = mail.rstrip('>')
                        domain = '?'
                        if mail.find('@') != -1:
                                domain = mail.rsplit('@', 1)[1]
                        date = datetime.datetime.fromtimestamp(float(stamp))

                        # First and last commit stamp (may be in any order because of cherry-picking and patches)
                        if stamp > self.last_commit_stamp:
                                self.last_commit_stamp = stamp
                        if self.first_commit_stamp == 0 or stamp < self.first_commit_stamp:
                                self.first_commit_stamp = stamp

                        # activity
                        # hour
                        hour = date.hour
                        self.activity_by_hour_of_day[hour] = self.activity_by_hour_of_day.get(hour, 0) + 1
                        # most active hour?
                        if self.activity_by_hour_of_day[hour] > self.activity_by_hour_of_day_busiest:
                                self.activity_by_hour_of_day_busiest = self.activity_by_hour_of_day[hour]

                        # day of week
                        day = date.weekday()
                        self.activity_by_day_of_week[day] = self.activity_by_day_of_week.get(day, 0) + 1

                        # domain stats
                        if domain not in self.domains:
                                self.domains[domain] = {}
                        # commits
                        self.domains[domain]['commits'] = self.domains[domain].get('commits', 0) + 1

                        # hour of week
                        if day not in self.activity_by_hour_of_week:
                                self.activity_by_hour_of_week[day] = {}
                        self.activity_by_hour_of_week[day][hour] = self.activity_by_hour_of_week[day].get(hour, 0) + 1
                        # most active hour?
                        if self.activity_by_hour_of_week[day][hour] > self.activity_by_hour_of_week_busiest:
                                self.activity_by_hour_of_week_busiest = self.activity_by_hour_of_week[day][hour]

                        # month of year
                        month = date.month
                        self.activity_by_month_of_year[month] = self.activity_by_month_of_year.get(month, 0) + 1

                        # yearly/weekly activity
                        yyw = date.strftime('%Y-%W')
                        self.activity_by_year_week[yyw] = self.activity_by_year_week.get(yyw, 0) + 1
                        if self.activity_by_year_week_peak < self.activity_by_year_week[yyw]:
                                self.activity_by_year_week_peak = self.activity_by_year_week[yyw]

                        # author stats
                        if author not in self.authors:
                                self.authors[author] = {}
                        # commits, note again that commits may be in any date order because of cherry-picking and patches
                        if 'last_commit_stamp' not in self.authors[author]:
                                self.authors[author]['last_commit_stamp'] = stamp
                        if stamp > self.authors[author]['last_commit_stamp']:
                                self.authors[author]['last_commit_stamp'] = stamp
                        if 'first_commit_stamp' not in self.authors[author]:
                                self.authors[author]['first_commit_stamp'] = stamp
                        if stamp < self.authors[author]['first_commit_stamp']:
                                self.authors[author]['first_commit_stamp'] = stamp

                        # author of the month/year
                        yymm = date.strftime('%Y-%m')
                        if yymm in self.author_of_month:
                                self.author_of_month[yymm][author] = self.author_of_month[yymm].get(author, 0) + 1
                        else:
                                self.author_of_month[yymm] = {}
                                self.author_of_month[yymm][author] = 1
                        self.commits_by_month[yymm] = self.commits_by_month.get(yymm, 0) + 1

                        if yymm in self.commits_by_type_by_month:
                                self.commits_by_type_by_month[yymm][commit_label] = self.commits_by_type_by_month[yymm].get(commit_label, 0) + 1
                        else:
                                self.commits_by_type_by_month[yymm] = {}
                                self.commits_by_type_by_month[yymm][commit_label] = 1

                        yy = date.year
                        if yy in self.author_of_year:
                                self.author_of_year[yy][author] = self.author_of_year[yy].get(author, 0) + 1
                        else:
                                self.author_of_year[yy] = {}
                                self.author_of_year[yy][author] = 1
                        self.commits_by_year[yy] = self.commits_by_year.get(yy, 0) + 1

                        if yy in self.commits_by_type_by_year:
                                self.commits_by_type_by_year[yy][commit_label] = self.commits_by_type_by_year[yy].get(commit_label, 0) + 1
                        else:
                                self.commits_by_type_by_year[yy] = {}
                                self.commits_by_type_by_year[yy][commit_label] = 1

                        # authors: active days
                        yymmdd = date.strftime('%Y-%m-%d')
                        if 'last_active_day' not in self.authors[author]:
                                self.authors[author]['last_active_day'] = yymmdd
                                self.authors[author]['active_days'] = set([yymmdd])
                        elif yymmdd != self.authors[author]['last_active_day']:
                                self.authors[author]['last_active_day'] = yymmdd
                                self.authors[author]['active_days'].add(yymmdd)

                        # project: active days
                        if yymmdd != self.last_active_day:
                                self.last_active_day = yymmdd
                                self.active_days.add(yymmdd)

                        # timezone
                        self.commits_by_timezone[timezone] = self.commits_by_timezone.get(timezone, 0) + 1

                # outputs "<stamp> <files>" for each revision
                revlines = getpipeoutput(['git rev-list --pretty=format:"%%ct %%T" %s' % getlogrange('HEAD'), 'grep -v ^commit']).strip().split('\n')
                lines = []
                revs_to_read = []
                time_rev_count = []
                #Look up rev in cache and take info from cache if found
                #If not append rev to list of rev to read from repo
                for revline in revlines:
                        time, rev = revline.split(' ')
                        #if cache empty then add time and rev to list of new rev's
                        #otherwise try to read needed info from cache
                        if 'files_in_tree' not in list(self.cache.keys()):
                                revs_to_read.append((time,rev))
                                continue
                        if rev in list(self.cache['files_in_tree'].keys()):
                                lines.append('%d %d' % (int(time), self.cache['files_in_tree'][rev]))
                        else:
                                revs_to_read.append((time,rev))

                #Read revisions from repo
                pool = Pool(processes=conf['processes'])
                time_rev_count = pool.map(getnumoffilesfromrev, revs_to_read)
                pool.terminate()
                pool.join()

                #Update cache with new revisions and append then to general list
                for (time, rev, count) in time_rev_count:
                        if 'files_in_tree' not in self.cache:
                                self.cache['files_in_tree'] = {}
                        self.cache['files_in_tree'][rev] = count
                        lines.append('%d %d' % (int(time), count))

                self.total_commits += len(lines)
                for line in lines:
                        parts = line.split(' ')
                        if len(parts) != 2:
                                continue
                        (stamp, files) = parts[0:2]
                        try:
                                self.files_by_stamp[int(stamp)] = int(files)
                        except ValueError:
                                print('Warning: failed to parse line "%s"' % line)

                # extensions and size of files
                lines = getpipeoutput(['git ls-tree -r -l -z %s' % getcommitrange('HEAD', end_only = True)]).split('\000')
                blobs_to_read = []
                for line in lines:
                        if len(line) == 0:
                                continue
                        parts = re.split(r'\s+', line, 4)
                        if parts[0] == '160000' and parts[3] == '-':
                                # skip submodules
                                continue
                        blob_id = parts[2]
                        size = int(parts[3])
                        fullpath = parts[4]

                        self.total_size += size
                        self.total_files += 1

                        filename = fullpath.split('/')[-1] # strip directories
                        if filename.find('.') == -1 or filename.rfind('.') == 0:
                                ext = ''
                        else:
                                ext = filename[(filename.rfind('.') + 1):]
                        if len(ext) > conf['max_ext_length']:
                                ext = ''
                        if ext not in self.extensions:
                                self.extensions[ext] = {'files': 0, 'lines': 0}
                        self.extensions[ext]['files'] += 1
                        #if cache empty then add ext and blob id to list of new blob's
                        #otherwise try to read needed info from cache
                        if 'lines_in_blob' not in list(self.cache.keys()):
                                blobs_to_read.append((ext,blob_id))
                                continue
                        if blob_id in list(self.cache['lines_in_blob'].keys()):
                                self.extensions[ext]['lines'] += self.cache['lines_in_blob'][blob_id]
                        else:
                                blobs_to_read.append((ext,blob_id))

                # TODO: filter by top level directory

                #Get info abount line count for new blob's that wasn't found in cache
                pool = Pool(processes=conf['processes'])
                ext_blob_linecount = pool.map(getnumoflinesinblob, blobs_to_read)
                pool.terminate()
                pool.join()

                #Update cache and write down info about number of number of lines
                for (ext, blob_id, linecount) in ext_blob_linecount:
                        if 'lines_in_blob' not in self.cache:
                                self.cache['lines_in_blob'] = {}
                        self.cache['lines_in_blob'][blob_id] = linecount
                        self.extensions[ext]['lines'] += self.cache['lines_in_blob'][blob_id]

                # line statistics
                # outputs:
                #  N files changed, N insertions (+), N deletions(-)
                # <stamp> <author>
                self.changes_by_date = {} # stamp -> { files, ins, del }
                # computation of lines of code by date is better done
                # on a linear history.
                extra = ''
                if conf['linear_linestats']:
                        extra = '--first-parent -m'
                lines = getpipeoutput(['git log --shortstat %s --pretty=format:"%%ct %%aN" %s' % (extra, getlogrange('HEAD'))]).split('\n')
                lines.reverse()
                files = 0; inserted = 0; deleted = 0; total_lines = 0
                author = None
                for line in lines:
                        if len(line) == 0:
                                continue

                        # <stamp> <author>
                        if re.search('files? changed', line) == None:
                                pos = line.find(' ')
                                if pos != -1:
                                        try:
                                                (stamp, author) = (int(line[:pos]), line[pos+1:])
                                                self.changes_by_date[stamp] = { 'files': files, 'ins': inserted, 'del': deleted, 'lines': total_lines }

                                                date = datetime.datetime.fromtimestamp(stamp)
                                                yymm = date.strftime('%Y-%m')
                                                self.lines_added_by_month[yymm] = self.lines_added_by_month.get(yymm, 0) + inserted
                                                self.lines_removed_by_month[yymm] = self.lines_removed_by_month.get(yymm, 0) + deleted

                                                yy = date.year
                                                self.lines_added_by_year[yy] = self.lines_added_by_year.get(yy,0) + inserted
                                                self.lines_removed_by_year[yy] = self.lines_removed_by_year.get(yy, 0) + deleted

                                                files, inserted, deleted = 0, 0, 0
                                        except ValueError:
                                                print('Warning: unexpected line "%s"' % line)
                                else:
                                        print('Warning: unexpected line "%s"' % line)
                        else:
                                numbers = getstatsummarycounts(line)

                                if len(numbers) == 3:
                                        (files, inserted, deleted) = [int(el) for el in numbers]
                                        total_lines += inserted
                                        total_lines -= deleted
                                        self.total_lines_added += inserted
                                        self.total_lines_removed += deleted

                                else:
                                        print('Warning: failed to handle line "%s"' % line)
                                        (files, inserted, deleted) = (0, 0, 0)
                                #self.changes_by_date[stamp] = { 'files': files, 'ins': inserted, 'del': deleted }
                self.total_lines += total_lines

                # Per-author statistics

                # defined for stamp, author only if author commited at this timestamp.
                self.changes_by_date_by_author = {} # stamp -> author -> lines_added

                # Similar to the above, but never use --first-parent
                # (we need to walk through every commit to know who
                # committed what, not just through mainline)
                lines = getpipeoutput(['git log --shortstat --date-order --pretty=format:"%%ct %%aN" %s' % (getlogrange('HEAD'))]).split('\n')
                lines.reverse()
                files = 0; inserted = 0; deleted = 0
                author = None
                stamp = 0
                for line in lines:
                        if len(line) == 0:
                                continue

                        # <stamp> <author>
                        if re.search('files? changed', line) == None:
                                pos = line.find(' ')
                                if pos != -1:
                                        try:
                                                oldstamp = stamp
                                                (stamp, author) = (int(line[:pos]), line[pos+1:])
                                                if oldstamp > stamp:
                                                        # clock skew, keep old timestamp to avoid having ugly graph
                                                        stamp = oldstamp
                                                if author not in self.authors:
                                                        self.authors[author] = { 'lines_added' : 0, 'lines_removed' : 0, 'commits' : 0}
                                                self.authors[author]['commits'] = self.authors[author].get('commits', 0) + 1
                                                self.authors[author]['lines_added'] = self.authors[author].get('lines_added', 0) + inserted
                                                self.authors[author]['lines_removed'] = self.authors[author].get('lines_removed', 0) + deleted
                                                if stamp not in self.changes_by_date_by_author:
                                                        self.changes_by_date_by_author[stamp] = {}
                                                if author not in self.changes_by_date_by_author[stamp]:
                                                        self.changes_by_date_by_author[stamp][author] = {}
                                                self.changes_by_date_by_author[stamp][author]['lines_added'] = self.authors[author]['lines_added']
                                                self.changes_by_date_by_author[stamp][author]['commits'] = self.authors[author]['commits']
                                                files, inserted, deleted = 0, 0, 0
                                        except ValueError:
                                                print('Warning: unexpected line "%s"' % line)
                                else:
                                        print('Warning: unexpected line "%s"' % line)
                        else:
                                numbers = getstatsummarycounts(line)

                                if len(numbers) == 3:
                                        (files, inserted, deleted) = [int(el) for el in numbers]
                                else:
                                        print('Warning: failed to handle line "%s"' % line)
                                        (files, inserted, deleted) = (0, 0, 0)

        def refine(self):
                # authors
                # name -> {place_by_commits, commits_frac, date_first, date_last, timedelta}
                self.authors_by_commits = getkeyssortedbyvaluekey(self.authors, 'commits')
                self.authors_by_commits.reverse() # most first
                for i, name in enumerate(self.authors_by_commits):
                        self.authors[name]['place_by_commits'] = i + 1

                for name in list(self.authors.keys()):
                        a = self.authors[name]
                        a['commits_frac'] = (100 * float(a['commits'])) / self.getTotalCommits()
                        date_first = datetime.datetime.fromtimestamp(a['first_commit_stamp'])
                        date_last = datetime.datetime.fromtimestamp(a['last_commit_stamp'])
                        delta = date_last - date_first
                        a['date_first'] = date_first.strftime('%Y-%m-%d')
                        a['date_last'] = date_last.strftime('%Y-%m-%d')
                        a['timedelta'] = delta
                        a['age_seconds'] = delta.total_seconds()
                        if 'lines_added' not in a: a['lines_added'] = 0
                        if 'lines_removed' not in a: a['lines_removed'] = 0

        def getActiveDays(self):
                return self.active_days

        def getActivityByDayOfWeek(self):
                return self.activity_by_day_of_week

        def getActivityByHourOfDay(self):
                return self.activity_by_hour_of_day

        def getAuthorInfo(self, author):
                return self.authors[author]

        def getAuthors(self, limit = None):
                res = getkeyssortedbyvaluekey(self.authors, 'commits')
                res.reverse()
                return res[:limit]

        def getCommitDeltaDays(self):
                return (self.last_commit_stamp / 86400 - self.first_commit_stamp / 86400) + 1

        def getDomainInfo(self, domain):
                return self.domains[domain]

        def getDomains(self):
                return list(self.domains.keys())

        def getFirstCommitDate(self):
                return datetime.datetime.fromtimestamp(self.first_commit_stamp)

        def getLastCommitDate(self):
                return datetime.datetime.fromtimestamp(self.last_commit_stamp)

        def getTags(self):
                lines = getpipeoutput(['git show-ref --tags', 'cut -d/ -f3'])
                return lines.split('\n')

        def getTagDate(self, tag):
                return self.revToDate('tags/' + tag)

        def getTotalAuthors(self):
                return self.total_authors

        def getTotalCommits(self):
                return self.total_commits

        def getTotalFiles(self):
                return self.total_files

        def getTotalLOC(self):
                return self.total_lines

        def getTotalSize(self):
                return self.total_size

        def revToDate(self, rev):
                stamp = int(getpipeoutput(['git log --pretty=format:%%ct "%s" -n 1' % rev]))
                return datetime.datetime.fromtimestamp(stamp).strftime('%Y-%m-%d')

class ReportCreator:
        """Creates the actual report based on given data."""
        def __init__(self):
                pass

        def create(self, data, path):
                self.data = data
                self.path = path

class JsonReportCreator(ReportCreator):
    def create(self, data, path):
        super().create(data, path)
        self.title = data.projectname

        root = Path(path)
        data_file = (root/'gitstats.js').open('w')
        time_format = '%Y-%m-%d %H:%M:%S'
        data_file.write(f"""
gen_date= "{datetime.datetime.now().strftime(time_format)}"
gen_duration = {time.time() - data.getStampCreated():.1f}
report_start = "{data.getFirstCommitDate().strftime(time_format)}"
report_end = "{data.getLastCommitDate().strftime(time_format)}"
age = {int(data.getCommitDeltaDays())}
active_days = {len(data.getActiveDays())}
active_prop = {100.0 * len(data.getActiveDays()) / data.getCommitDeltaDays():.2f}
number_files = {data.getTotalFiles()}
number_lines = {data.getTotalLOC()}
lines_added = {data.total_lines_added}
lines_removed = {data.total_lines_removed}
total_commits = {data.getTotalCommits()}
total_commits_by_label = {json.dumps(data.commits_by_label, separators=(',', ':'))}
avg_commits_per_active_day = {float(data.getTotalCommits()) / len(data.getActiveDays()):.1f}
avg_commits_per_day = {float(data.getTotalCommits()) / data.getCommitDeltaDays():.1f}
nb_authors = {data.getTotalAuthors()}
avg_commits_by_author = {data.getTotalCommits()/data.getTotalAuthors():.1f}
""")

        ### Weekly activity for the past WEEKS weeks
        WEEKS = 32

        # generate weeks to show (previous N weeks from now)
        now = datetime.datetime.now()
        deltaweek = datetime.timedelta(7)

        weeks = [(now + (i-WEEKS+1)*deltaweek).strftime('%Y-%W')
                 for i in range(WEEKS)]
        jdata = { 'labels' : weeks,
                  'data': [data.activity_by_year_week.get(weeks[i], 0)
                             for i in range(0, WEEKS)]}
        data_file.write(f'weekly_activity = {json.dumps(jdata)}\n')

        ### Hour of day
        totalcommits = data.getTotalCommits()
        hour_of_day = data.getActivityByHourOfDay()
        hours = range(24)
        jdata = { 'labels' : list(hours),
                  'data': [float(hour_of_day.get(hour, 0))/totalcommits for hour in hours]}
        data_file.write(f'hour_of_day = {json.dumps(jdata)}\n')

        ### Day of week
        day_of_week = data.getActivityByDayOfWeek()
        days = range(7)
        jdata = { 'labels' : list(WEEKDAYS),
                  'data': [100.0*float(day_of_week.get(day, 0))/totalcommits
                      for day in days]}
        data_file.write(f'day_of_week = {json.dumps(jdata)}\n')

        ### Hour of week missing (pure table)

        ### Month of year
        month_of_year = data.activity_by_month_of_year
        months = range(1, 13)
        month_names = [ 'Jan.', 'Feb.', 'Mar.', 'Apr.',' May', 'Jun.', 'Jul.',
                'Aug.', 'Sep.', 'Oct.', 'Nov.', 'Dec.']
        jdata = { 'labels' : month_names,
                  'data': [month_of_year.get(month, 0) for month in months]}
        data_file.write(f'month_of_year = {json.dumps(jdata)}\n')

        ### Commits by year/month  FIXME: add missing months in between
        yymms = sorted(data.commits_by_month.keys())
        jdata = { 'labels' : list(yymms),
                  'data': [data.commits_by_month[yymm] for yymm in yymms]}
        data_file.write(f'by_year_month = {json.dumps(jdata)}\n')

        jdata = {}
        for commit_label in data.commits_by_label.keys():
                jdata[commit_label] = { 'labels' : list(yymms),
                        'data': [data.commits_by_type_by_month[yymm].get(commit_label, 0) for yymm in yymms]}
        data_file.write(f'by_labels_by_year_month = {json.dumps(jdata)}\n')

        ### Commits by year
        years = sorted(data.commits_by_year.keys())
        jdata = { 'labels' : list(years),
                  'data': [data.commits_by_year[year] for year in years]}
        data_file.write(f'by_year = {json.dumps(jdata)}\n')

        jdata = {}
        for commit_label in data.commits_by_label.keys():
                jdata[commit_label] = { 'labels' : list(years),
                        'data': [data.commits_by_type_by_year[year].get(commit_label, 0) for year in years]}
        data_file.write(f'by_labels_by_year = {json.dumps(jdata)}\n')

        ### By time zone missing (pure table)

        ### Custom sorting functions
        data_file.write("""function sort_commits(a, b) {
    const commitsA = parseInt(a.split(' '));
    const commitsB = parseInt(b.split(' '));
    return commitsA - commitsB;
}
function sort_age(a, b, rowA, rowB) {
    const ageA = rowA.getData().age_seconds;
    const ageB = rowB.getData().age_seconds;
    return ageA - ageB;
}
""")
        ### Authors
        data_file.write('authors_cols = ['
                '{formatter: "rownum", width: 40 },'
                '{title: "Author", field: "Author"},'
                '{title: "Commits (%)", field: "Commits (%)", sorter: sort_commits},'
                '{title: "+ lines", field: "+ lines"},'
                '{title: "- lines", field: "- lines"},'
                '{title: "First commit", field: "First commit"},'
                '{title: "Last commit", field: "Last commit"},'
                '{title: "Age", field: "Age", sorter: sort_age},'
                '{title: "age_seconds", field: "age_seconds", visible: false},'
                '{title: "Active days", field: "Active days"}]\n')
        authors = []
        for id_, author in enumerate(data.getAuthors()):
            if author == 'leanprover-community-bot':
                continue
            info = data.getAuthorInfo(author)
            row = { 'id': id_ + 1,
                    'Author': author,
                    'Commits (%)': f"{info['commits']} ({info['commits_frac']})",
                    '+ lines': info['lines_added'],
                    '- lines': info['lines_removed'],
                    'First commit': info['date_first'],
                    'Last commit': info['date_last'],
                    'Age': str(info['timedelta']),
                    'age_seconds': info['age_seconds'],
                    'Active days': len(info['active_days'])}
            authors.append(row)
        data_file.write(f'authors = {json.dumps(authors)}\n')

        ### Lines and commits by author
        lines_by_authors = {}
        commits_by_authors = {}
        cda = data.changes_by_date_by_author
        self.authors_to_plot = data.getAuthors(conf['max_authors'])
        for author in self.authors_to_plot:
            if author == 'leanprover-community-bot':
                continue
            lines_by_authors[author] = []
            commits_by_authors[author] = []
        for stamp in sorted(cda):
            for author in self.authors_to_plot:
                if author == 'leanprover-community-bot':
                    continue
                if author in cda[stamp]:
                    lines_by_authors[author].append(
                            {'x': stamp,
                             'y': cda[stamp][author]['lines_added']})
                    commits_by_authors[author].append(
                            {'x': stamp,
                             'y': cda[stamp][author]['commits']})
        data_file.write(f'stamps = {list(cda.keys())}\n')
        data_file.write(f'lines_by_authors = {json.dumps(lines_by_authors)}\n')
        data_file.write(f'commits_by_authors = {json.dumps(commits_by_authors)}\n')

        ### Author of Month
        next_top = f"Next top {conf['authors_top']}"
        data_file.write('aom_cols = ['
                '{title: "Month", field: "month"},'
                '{title: "Author", field: "author"},'
                '{title: "Commits (%)", field: "commits"},'
                f'{{title: "{next_top}", field: "next_top"}},'
                '{title: "Number of authors", field: "number"}]\n')

        rows = []
        for id_, yymm in enumerate(reversed(sorted(data.author_of_month))):
            authordict = data.author_of_month[yymm]
            authors = getkeyssortedbyvalues(authordict)
            authors.reverse()
            commits = data.author_of_month[yymm][authors[0]]
            total = data.commits_by_month[yymm]
            prop = (100.0 * commits)/data.commits_by_month[yymm]
            nexts = ', '.join(authors[1:conf['authors_top']+1])
            row = { 'id': id_+1,
                    'month': yymm,
                    'author': authors[0],
                    'commits': f"{commits} ({prop:.2f}% of {total})",
                    'next_top': nexts,
                    'number': len(authors) }
            rows.append(row)

        data_file.write(f'aom = {json.dumps(rows)}\n')

        ### Author of Year
        data_file.write('aoy_cols = ['
                '{title: "Year", field: "year"},'
                '{title: "Author", field: "author"},'
                '{title: "Commits (%)", field: "commits"},'
                f'{{title: "{next_top}", field: "next_top"}},'
                '{title: "Number of authors", field: "number"}]\n')

        rows = []
        for id_, yy in enumerate(reversed(sorted(data.author_of_year))):
            authordict = data.author_of_year[yy]
            authors = getkeyssortedbyvalues(authordict)
            authors.reverse()
            commits = data.author_of_year[yy][authors[0]]
            total = data.commits_by_year[yy]
            prop = (100.0 * commits)/data.commits_by_year[yy]
            nexts = ', '.join(authors[1:conf['authors_top']+1])
            row = { 'id': id_+1,
                    'year': yy,
                    'author': authors[0],
                    'commits': f"{commits} ({(100.0 * commits)/data.commits_by_year[yy]:.2f}% of {data.commits_by_year[yy]})",
                    'next_top': nexts,
                    'number': len(authors) }
            rows.append(row)

        data_file.write(f'aoy = {json.dumps(rows)}\n')

        ### Number of files by date
        dates = []
        files = []
        cur_nb = -1
        for stamp, nb in sorted(data.files_by_stamp.items()):
            if nb != cur_nb:
                dates.append(stamp)
                files.append(nb)
                cur_nb = nb

        ### TODO: do for each top level directory

        data_file.write( f"files_stamps = {dates}\n")
        data_file.write( f"files_by_date = {{'Number of files': {files}}}\n")

        ### Number of lines by date
        dates = []
        lines = []
        cur_nb = -1
        for stamp, nb in sorted(data.changes_by_date.items()):
            if nb['lines'] != cur_nb:
                cur_nb = nb['lines']
                dates.append(stamp)
                lines.append(cur_nb)

        data_file.write( f"lines_stamps = {dates}\n")
        data_file.write( f"lines_by_date = {{'Number of lines': {lines}}}\n")



        data_file.close()


def usage():
        print("""
Usage: gitstats [options] <gitpath..> <outputpath>

Options:
-c key=value     Override configuration value

Default config values:
%s

Please see the manual page for more details.
""" % conf)


class GitStats:
        def run(self, args_orig, Reporter=JsonReportCreator):
                optlist, args = getopt.getopt(args_orig, 'hc:', ["help"])
                for o,v in optlist:
                        if o == '-c':
                                key, value = v.split('=', 1)
                                if key not in conf:
                                        raise KeyError('no such key "%s" in config' % key)
                                if isinstance(conf[key], int):
                                        conf[key] = int(value)
                                else:
                                        conf[key] = value
                        elif o in ('-h', '--help'):
                                usage()
                                sys.exit()

                if len(args) < 2:
                        usage()
                        sys.exit(0)

                outputpath = os.path.abspath(args[-1])
                rundir = os.getcwd()

                try:
                        os.makedirs(outputpath)
                except OSError:
                        pass
                if not os.path.isdir(outputpath):
                        print('FATAL: Output path is not a directory or does not exist')
                        sys.exit(1)

                print('Output path: %s' % outputpath)
                cachefile = os.path.join(outputpath, 'gitstats.cache')

                data = GitDataCollector()
                data.loadCache(cachefile)

                for gitpath in args[0:-1]:
                        print('Git path: %s' % gitpath)

                        prevdir = os.getcwd()
                        os.chdir(gitpath)

                        print('Collecting data...')
                        data.collect(gitpath)

                        os.chdir(prevdir)

                print('Refining data...')
                data.saveCache(cachefile)
                data.refine()

                os.chdir(rundir)

                print('Generating report...')
                report = Reporter()
                report.create(data, outputpath)

                time_end = time.time()
                exectime_internal = time_end - time_start
                print('Execution time %.5f secs, %.5f secs (%.2f %%) in external commands)' % (exectime_internal, exectime_external, (100.0 * exectime_external) / exectime_internal))
                if sys.stdin.isatty():
                        print('You may now run:')
                        print()
                        print('   sensible-browser \'%s\'' % os.path.join(outputpath, 'index.html').replace("'", "'\\''"))
                        print()

if __name__=='__main__':
        g = GitStats()
        g.run(sys.argv[1:], JsonReportCreator)


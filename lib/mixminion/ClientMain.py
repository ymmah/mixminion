# Copyright 2002-2003 Nick Mathewson.  See LICENSE for licensing information.
# $Id: ClientMain.py,v 1.52 2003/02/12 01:22:57 nickm Exp $

"""mixminion.ClientMain

   Code for Mixminion command-line client.
   """

__all__ = [ 'Address', 'ClientKeyring', 'ClientDirectory', 'MixminionClient',
    'parsePath', ]

import anydbm
import binascii
import cPickle
import getopt
import getpass
import os
import stat
import sys
import time
import urllib
from types import ListType

import mixminion.BuildMessage
import mixminion.Crypto
import mixminion.MMTPClient
from mixminion.Common import IntervalSet, LOG, floorDiv, MixError, \
     MixFatalError, MixProtocolError, ceilDiv, createPrivateDir, \
     isSMTPMailbox, formatDate, formatFnameTime, formatTime, Lockfile, \
     openUnique, previousMidnight, readPossiblyGzippedFile, secureDelete, \
     stringContains, succeedingMidnight
from mixminion.Crypto import sha1, ctr_crypt, trng
from mixminion.Config import ClientConfig, ConfigError
from mixminion.ServerInfo import ServerInfo, ServerDirectory
from mixminion.Packet import ParseError, parseMBOXInfo, parseReplyBlocks, \
     parseSMTPInfo, parseTextEncodedMessage, parseTextReplyBlocks, ReplyBlock,\
     MBOX_TYPE, SMTP_TYPE, DROP_TYPE

# FFFF This should be made configurable and adjustable.
MIXMINION_DIRECTORY_URL = "http://www.mixminion.net/directory/latest.gz"
MIXMINION_DIRECTORY_FINGERPRINT = "CD80DD1B8BE7CA2E13C928D57499992D56579CCD"

# Global variable; holds an instance of Common.Lockfile used to prevent
# concurrent access to the directory cache, message pool, or SURB log.
_CLIENT_LOCKFILE = None

def clientLock():
    """DOCDOC"""
    assert _CLIENT_LOCKFILE is not None
    _CLIENT_LOCKFILE.acquire(blocking=1)

def clientUnlock():
    """DOCDOC"""    
    _CLIENT_LOCKFILE.release()

def configureClientLock(filename):
    """DOCDOC"""    
    global _CLIENT_LOCKFILE
    _CLIENT_LOCKFILE = Lockfile(filename)

class UIError(MixError):
    "DOCDOC"
    def dump(self):
        if str(self): print "ERROR:", str(self)

class UsageError(UIError):
    "DOCDOC"

class ClientDirectory:
    """A ClientDirectory manages a list of server descriptors, either
       imported from the command line or from a directory."""
    ##Fields:
    # dir: directory where we store everything.
    # lastModified: time when we last modified this keystore
    # lastDownload: time when we last downloaded a directory
    # serverList: List of (ServerInfo, 'D'|'I:filename') tuples.  The
    #   second element indicates whether the ServerInfo comes from a
    #   directory or a file.
    # digestMap: Map of (Digest -> 'D'|'I:filename').
    # byNickname: Map from nickname.lower() to list of (ServerInfo, source)
    #   tuples.
    # byCapability: Map from capability ('mbox'/'smtp'/'relay'/None) to
    #    list of (ServerInfo, source) tuples.
    # allServers: Same as byCapability[None]
    # __scanning: Flag to prevent recursive invocation of self.rescan().
    ## Layout:
    # DIR/cache: A cPickled tuple of ("ClientKeystore-0",
    #         lastModified, lastDownload, serverlist, digestMap)
    # DIR/dir.gz *or* DIR/dir: A (possibly gzipped) directory file.
    # DIR/imported/: A directory of server descriptors.
    MAGIC = "ClientKeystore-0"

    #DOCDOC
    DEFAULT_REQUIRED_LIFETIME = 3600

    def __init__(self, directory):
        """Create a new ClientDirectory to keep directories and descriptors
           under <directory>."""
        self.dir = directory
        createPrivateDir(self.dir)
        createPrivateDir(os.path.join(self.dir, "imported"))
        self.digestMap = {}
        self.__scanning = 0
        try:
            clientLock()
            self.__load()
            self.clean()
        finally:
            clientUnlock()

        # Mixminion 0.0.1 used an obsolete directory-full-of-servers in
        #   DIR/servers.  If there's nothing there, we remove it.  Otherwise,
        #   we warn.
        sdir = os.path.join(self.dir,"servers")
        if os.path.exists(sdir):
            if os.listdir(sdir):
                LOG.warn("Skipping obsolete server directory %s", sdir)
            else:
                try:
                    LOG.warn("Removing obsolete server directory %s", sdir)
                    os.rmdir(sdir)
                except OSError, e:
                    LOG.warn("Failed: %s", e)

    def updateDirectory(self, forceDownload=0, now=None):
        """Download a directory from the network as needed."""
        if now is None:
            now = time.time()

        if forceDownload or self.lastDownload < previousMidnight(now):
            self.downloadDirectory()
        else:
            LOG.debug("Directory is up to date.")

    def downloadDirectory(self):
        """Download a new directory from the network, validate it, and
           rescan its servers."""
        # Start downloading the directory.
        url = MIXMINION_DIRECTORY_URL
        LOG.info("Downloading directory from %s", url)
        try:
            infile = urllib.FancyURLopener().open(url)
        except IOError, e:
            raise MixError("Couldn't connect to directory server: %s"%e)
        # Open a temporary output file.
        if url.endswith(".gz"):
            fname = os.path.join(self.dir, "dir_new.gz")
            outfile = open(fname, 'wb')
            gz = 1
        else:
            fname = os.path.join(self.dir, "dir_new")
            outfile = open(fname, 'w')
            gz = 0
        # Read the file off the network.
        while 1:
            s = infile.read(1<<16)
            if not s: break
            outfile.write(s)
        # Close open connections.
        infile.close()
        outfile.close()
        # Open and validate the directory
        LOG.info("Validating directory")
        try:
            directory = ServerDirectory(fname=fname,
                                        validatedDigests=self.digestMap)
        except ConfigError, e:
            raise MixFatalError("Downloaded invalid directory: %s" % e)

        # Make sure that the identity is as expected.
        identity = directory['Signature']['DirectoryIdentity']
        fp = MIXMINION_DIRECTORY_FINGERPRINT
        if fp and mixminion.Crypto.pk_fingerprint(identity) != fp:
            raise MixFatalError("Bad identity key on directory")

        try:
            os.unlink(os.path.join(self.dir, "cache"))
        except OSError:
            pass

        # Install the new directory
        if gz:
            os.rename(fname, os.path.join(self.dir, "dir.gz"))
        else:
            os.rename(fname, os.path.join(self.dir, "dir"))

        # And regenerate the cache.
        self.rescan()
        # FFFF Actually, we could be a bit more clever here, and same some
        # FFFF time. But that's for later.

    def rescan(self, force=None, now=None):
        """Regenerate the cache based on files on the disk."""
        self.lastModified = self.lastDownload = -1
        self.serverList = []
        if force:
            self.digestMap = {}

        # Read the servers from the directory.
        gzipFile = os.path.join(self.dir, "dir.gz")
        dirFile = os.path.join(self.dir, "dir")
        for fname in gzipFile, dirFile:
            if not os.path.exists(fname): continue
            self.lastDownload = self.lastModified = \
                                os.stat(fname)[stat.ST_MTIME]
            try:
                directory = ServerDirectory(fname=fname,
                                            validatedDigests=self.digestMap)
            except ConfigError:
                LOG.warn("Ignoring invalid directory (!)")
                continue

            for s in directory.getServers():
                self.serverList.append((s, 'D'))
                self.digestMap[s.getDigest()] = 'D'
            break

        # Now check the server in DIR/servers.
        serverDir = os.path.join(self.dir, "imported")
        createPrivateDir(serverDir)
        for fn in os.listdir(serverDir):
            # Try to read a file: is it a server descriptor?
            p = os.path.join(serverDir, fn)
            try:
                # Use validatedDigests *only* when not explicitly forced.
                info = ServerInfo(fname=p, assumeValid=0,
                                  validatedDigests=self.digestMap)
            except ConfigError:
                LOG.warn("Invalid server descriptor %s", p)
                continue
            mtime = os.stat(p)[stat.ST_MTIME]
            if mtime > self.lastModified:
                self.lastModifed = mtime
            self.serverList.append((info, "I:%s"%fn))
            self.digestMap[info.getDigest()] = "I:%s"%fn

        # Regenerate the cache
        self.__save()
        # Now try reloading, to make sure we can, and to get __rebuildTables.
        self.__scanning = 1
        self.__load()

    def __load(self):
        """Helper method. Read the cached parsed descriptors from disk."""
        try:
            f = open(os.path.join(self.dir, "cache"), 'rb')
            cached = cPickle.load(f)
            magic, self.lastModified, self.lastDownload, self.serverList, \
                   self.digestMap = cached
            f.close()
            if magic == self.MAGIC:
                self.__rebuildTables()
                return
            else:
                LOG.warn("Bad magic on keystore cache; rebuilding...")
        except (OSError, IOError):
            LOG.info("Couldn't read server cache; rebuilding")
        except (cPickle.UnpicklingError, ValueError), e:
            LOG.info("Couldn't unpickle server cache: %s", e)
        if self.__scanning:
            raise MixFatalError("Recursive error while regenerating cache")
        self.rescan()

    def __save(self):
        """Helper method. Recreate the cache on disk."""
        fname = os.path.join(self.dir, "cache.new")
        try:
            os.unlink(fname)
        except OSError:
            pass
        f = open(fname, 'wb')
        cPickle.dump((self.MAGIC,
                      self.lastModified, self.lastDownload, self.serverList,
                      self.digestMap),
                     f, 1)
        f.close()
        os.rename(fname, os.path.join(self.dir, "cache"))

    def importFromFile(self, filename):
        """Import a new server descriptor stored in 'filename'"""

        contents = readPossiblyGzippedFile(filename)
        info = ServerInfo(string=contents, validatedDigests=self.digestMap)

        nickname = info.getNickname()
        lcnickname = nickname.lower()
        identity = info.getIdentity()
        # Make sure that the identity key is consistent with what we know.
        for s, _ in self.serverList:
            if s.getNickname() == nickname:
                if not mixminion.Crypto.pk_same_public_key(identity,
                                                           s.getIdentity()):
                    raise MixError("Identity key changed for server %s in %s",
                                   nickname, filename)

        # Have we already imported this server?
        if self.digestMap.get(info.getDigest(), "X").startswith("I:"):
            raise MixError("Server descriptor is already imported")

        # Is the server expired?
        if info.isExpiredAt(time.time()):
            raise MixError("Server desciptor is expired")

        # Is the server superseded?
        if self.byNickname.has_key(lcnickname):
            if info.isSupersededBy([s for s,_ in self.byNickname[lcnickname]]):
                raise MixError("Server descriptor is superseded")

        # Copy the server into DIR/servers.
        fnshort = "%s-%s"%(nickname, formatFnameTime())
        fname = os.path.join(self.dir, "imported", fnshort)
        f = openUnique(fname)[0]
        f.write(contents)
        f.close()
        # Now store into the cache.
        fnshort = os.path.split(fname)[1]
        self.serverList.append((info, 'I:%s'%fnshort))
        self.digestMap[info.getDigest()] = 'I:%s'%fnshort
        self.lastModified = time.time()
        self.__save()
        self.__rebuildTables()

    def expungeByNickname(self, nickname):
        """Remove all imported (non-directory) server nicknamed 'nickname'."""
        lcnickname = nickname.lower()
        n = 0 # number removed
        newList = [] # replacement for serverList.

        for info, source in self.serverList:
            if source == 'D' or info.getNickname().lower() != lcnickname:
                newList.append((info, source))
                continue
            n += 1
            try:
                fn = source[2:]
                os.unlink(os.path.join(self.dir, "imported", fn))
            except OSError, e:
                LOG.error("Couldn't remove %s: %s", fn, e)

        self.serverList = newList
        # Recreate cache if needed.
        if n:
            self.lastModifed = time.time()
            self.__save()
            self.__rebuildTables()
        return n

    def __rebuildTables(self):
        """Helper method.  Reconstruct byNickname, allServers, and byCapability
           from the internal start of this object.
        """
        self.byNickname = {}
        self.allServers = []
        self.byCapability = { 'mbox': [],
                              'smtp': [],
                              'relay': [],
                              None: self.allServers }

        for info, where in self.serverList:
            nn = info.getNickname().lower()
            lists = [ self.allServers, self.byNickname.setdefault(nn, []) ]
            for c in info.getCaps():
                lists.append( self.byCapability[c] )
            for lst in lists:
                lst.append((info, where))

    def listServers(self):
        """Returns a linewise listing of the current servers and their caps.
            This will go away or get refactored in future versions once we
            have client-level modules."""
        lines = []
        nicknames = self.byNickname.keys()
        nicknames.sort()
        if not nicknames:
            return [ "No servers known" ]
        longestnamelen = max(map(len, nicknames))
        fmtlen = min(longestnamelen, 20)
        format = "%"+str(fmtlen)+"s:"
        for n in nicknames:
            nnreal = self.byNickname[n][0][0].getNickname()
            lines.append(format%nnreal)
            for info, where in self.byNickname[n]:
                caps = info.getCaps()
                va = formatDate(info['Server']['Valid-After'])
                vu = formatDate(info['Server']['Valid-Until'])
                line = "   %15s (valid %s to %s)"%(" ".join(caps),va,vu)
                lines.append(line)
        return lines

    def __findOne(self, lst, startAt, endAt):
        """Helper method.  Given a list of (ServerInfo, where), return a
           single element that is valid for all time between startAt and
           endAt.

           Watch out: this element is _not_ randomly chosen.
           """
        res = self.__find(lst, startAt, endAt)
        if res:
            return res[0]
        return None

    def __find(self, lst, startAt, endAt):
        """Helper method.  Given a list of (ServerInfo, where), return all
           elements that are valid for all time between startAt and endAt.

           Only one element is returned for each nickname; if multiple
           elements with a given nickname are valid over the given time
           interval, the most-recently-published one is included.
           """
        # XXXX This is not really good: servers may be the same, even if
        # XXXX their nicknames are different.  The logic should probably
        # XXXX go into directory, though.

        u = {} # Map from lcnickname -> latest-expiring info encountered in lst
        for info, _  in lst:
            if not info.isValidFrom(startAt, endAt):
                continue
            n = info.getNickname().lower()
            if u.has_key(n):
                if u[n].isNewerThan(info):
                    continue
            u[n] = info

        return u.values()

    def clean(self, now=None):
        """Remove all expired or superseded descriptors from DIR/servers."""

        if now is None:
            now = time.time()
        cutoff = now - 600

        #DOCDOC
        newServers = []
        for info, where in self.serverList:
            lcnickname = info.getNickname().lower()
            others = [ s for s, _ in self.byNickname[lcnickname] ]
            inDirectory = [ s.getDigest()
                            for s, w in self.byNickname[lcnickname]
                            if w == 'D' ]
            if (where != 'D'
                and (info.isExpiredAt(cutoff)
                     or info.isSupersededBy(others)
                     or info.getDigest() in inDirectory)):
                # If the descriptor is not in the directory, and it is
                # expired, is superseded, or is duplicated by a descriptor
                # from the directory, remove it.
                try:
                    os.unlink(os.path.join(self.dir, "imported", where[2:]))
                except OSError, e:
                    LOG.info("Couldn't remove %s: %s", where[2:], e)
            else:
                # Don't scratch non-superseded, non-expired servers.
                newServers.append((info, where))

        if len(self.serverList) != len(newServers):
            self.serverList = newServers
            self.__save()
            self.__rebuildTables()

    def getServerInfo(self, name, startAt=None, endAt=None, strict=0):
        """Return the most-recently-published ServerInfo for a given
           'name' valid over a given time range.  If strict, and no
           such server is found, return None.

           name -- A ServerInfo object, a nickname, or a filename.
           """

        if startAt is None:
            startAt = time.time()
        if endAt is None:
            endAt = startAt + self.DEFAULT_REQUIRED_LIFETIME

        #DOCDOC
        if isinstance(name, ServerInfo):
            if name.isValidFrom(startAt, endAt):
                return name
            else:
                LOG.error("Server is not currently valid")
        elif self.byNickname.has_key(name.lower()):
            s = self.__findOne(self.byNickname[name.lower()], startAt, endAt)
            if not s:
                raise MixError("Couldn't find valid descriptor %s" % name)
            return s
        elif os.path.exists(os.path.expanduser(name)):
            fname = os.path.expanduser(name)
            try:
                return ServerInfo(fname=fname, assumeValid=0)
            except OSError, e:
                raise MixError("Couldn't read descriptor %r: %s" %
                               (name, e))
            except ConfigError, e:
                raise MixError("Couldn't parse descriptor %r: %s" %
                               (name, e))
        elif strict:
            raise MixError("Couldn't find descriptor %r" % name)
        else:
            return None

    def getPath(self, midCap=None, endCap=None, length=None,
                startServers=(), endServers=(),
                startAt=None, endAt=None, prng=None):
        """Workhorse method for path selection.  Constructs a path of length
           >= 'length' hops, path, beginning with startServers and ending with
           endServers.  If more servers are required to make up 'length' hops,
           they are selected at random.

           All servers are chosen to be valid continuously from startAt to
           endAt.  All newly-selected servers except the last are required to
           have 'midCap' (typically 'relay'); the last server (if endServers
           is not set) is selected to have 'endCap' (typically 'mbox' or
           'smtp').

           The path selection algorithm is a little complicated, but gets
           more reasonable as we know about more servers.
        """
        if startAt is None:
            startAt = time.time()
        if endAt is None:
            endAt = startAt + self.DEFAULT_REQUIRED_LIFETIME
        if prng is None:
            prng = mixminion.Crypto.getCommonPRNG()

        # Look up the manditory servers.
        startServers = [ self.getServerInfo(name,startAt,endAt,1)
                         for name in startServers ]
        endServers = [ self.getServerInfo(name,startAt,endAt,1)
                       for name in endServers ]

        # Are we done?
        nNeeded = 0
        if length:
            nNeeded = length - len(startServers) - len(endServers)

        if nNeeded <= 0:
            return startServers + endServers

        # Do we need to specify the final server in the path?
        if not endServers:
            # If so, find all candidates...
            endList = self.__find(self.byCapability[endCap],startAt,endAt)
            if not endList:
                raise MixError("No %s servers known" % endCap)
            # ... and pick one that hasn't been used, if possible.
            used = [ info.getNickname().lower() for info in startServers ]
            unusedEndList = [ info for info in endList
                              if info.getNickname().lower() not in used ]
            if unusedEndList:
                endServers = [ prng.pick(unusedEndList) ]
            else:
                endServers = [ prng.pick(endList) ]
            LOG.debug("Chose %s at exit server", endServers[0].getNickname())
            nNeeded -= 1

        # Now are we done?
        if nNeeded == 0:
            return startServers + endServers

        # This is hard.  We need to find a number of relay servers for
        # the midddle of the list.  If len(candidates) > length, we should
        # remove all servers that already appear, and shuffle from the
        # rest.  Otherwise, if len(candidates) >= 3, we pick one-by-one from
        # the list of possibilities, just making sure not to get 2 in a row.
        # Otherwise, len(candidates) <= 3, so we just wing it.
        #
        # FFFF This algorithm is far from ideal, but the answer is to
        # FFFF get more servers deployed.

        # Find our candidate servers.
        midList = self.__find(self.byCapability[midCap],startAt,endAt)
        # Which of them are we using now?
        used = [ info.getNickname().lower()
                 for info in list(startServers)+list(endServers) ]
        # Which are left?
        unusedMidList = [ info for info in midList
                          if info.getNickname().lower() not in used ]
        if len(unusedMidList) >= nNeeded:
            # We have enough enough servers to choose without replacement.
            midServers = prng.shuffle(unusedMidList, nNeeded)
        elif len(midList) >= 3:
            # We have enough servers to choose without two hops in a row to
            # the same server.
            LOG.warn("Not enough servers for distinct path (%s unused, %s known)",
                     len(unusedMidList), len(midList))

            midServers = []
            if startServers:
                prevNickname = startServers[-1].getNickname().lower()
            else:
                prevNickname = " (impossible nickname) "
            if endServers:
                endNickname = endServers[0].getNickname().lower()
            else:
                endNickname = " (impossible nickname) "

            while nNeeded:
                info = prng.pick(midList)
                n = info.getNickname().lower()
                if n != prevNickname and (nNeeded > 1 or n != endNickname):
                    midServers.append(info)
                    prevNickname = n
                    nNeeded -= 1
        elif len(midList) == 2:
            # We have enough servers to concoct a path that at least
            # _sometimes_ doesn't go to the same server twice in a row.
            LOG.warn("Not enough relays to avoid same-server hops")
            midList = prng.shuffle(midList)
            midServers = (midList * ceilDiv(nNeeded, 2))[:nNeeded]
        elif len(midList) == 1:
            # There's no point in choosing a long path here: it can only
            # have one server in it.
            LOG.warn("Only one relay known")
            midServers = midList
        else:
            # We don't know any servers at all.
            raise MixError("No relays known")

        LOG.debug("getPath: [%s][%s][%s]",
                  " ".join([ s.getNickname() for s in startServers ]),
                  " ".join([ s.getNickname() for s in midServers   ]),
                  " ".join([ s.getNickname() for s in endServers   ]))

        return startServers + midServers + endServers

def resolvePath(keystore, address, enterPath, exitPath,
                nHops, nSwap, startAt=None, endAt=None, halfPath=0):
    """Compute a two-leg validated path from options as entered on
       the command line.

       Otherwise, we generate an nHops-hop path, swapping at the nSwap'th
       server, starting with the servers on enterPath, and finishing with the
       servers on exitPath (followed by the server, if any, mandated by
       address.

       All descriptors chosen are valid from startAt to endAt.  If the
       specified descriptors don't support the required capabilities,
       we raise MixError.

       DOCDOC halfPath
       """
    # First, find out what the exit node needs to be (or support).
    if address is None:
        routingType = None
        exitNode = None
    else:
        routingType, _, exitNode = address.getRouting()
        
    if exitNode:
        exitNode = keystore.getServerInfo(exitNode, startAt, endAt)
    if routingType == MBOX_TYPE:
        exitCap = 'mbox'
    elif routingType == SMTP_TYPE:
        exitCap = 'smtp'
    else:
        exitCap = None

    # We have a normally-specified path.
    if exitNode is not None:
        exitPath = exitPath[:]
        exitPath.append(exitNode)

    path = keystore.getPath(length=nHops,
                            startServers=enterPath,
                            endServers=exitPath,
                            midCap='relay', endCap=exitCap,
                            startAt=startAt, endAt=endAt)

    #DOCDOC
    for server in path[:-1]:
        if "relay" not in server.getCaps():
            raise MixError("Server %s does not support relay"
                           % server.getNickname())
    #DOCDOC
    if exitCap and exitCap not in path[-1].getCaps():
        raise MixError("Server %s does not support %s"
                       % (path[-1].getNickname(), exitCap))

    #DOCDOC
    if nSwap is None:
        nSwap = ceilDiv(len(path),2)-1

    path1, path2 = path[:nSwap+1], path[nSwap+1:]
    if not halfPath and (not path1 or not path2):
        raise MixError("Each leg of the path must have at least 1 hop")
    return path1, path2

def parsePath(keystore, config, path, address, nHops=None,
              nSwap=None, startAt=None, endAt=None, halfPath=0,
              defaultNHops=None):
    """Resolve a path as specified on the command line.  Returns a
       (path-leg-1, path-leg-2) tuple.

       keystore -- the keystore to use.
       config -- unused for now.
       path -- the path, in a format described below.  If the path is
          None, all servers are chosen as if the path were '*'.
       address -- the address to deliver the message to; if it specifies
          an exit node, the exit node is appended to the second leg of the
          path and does not count against the number of hops.
       nHops -- the number of hops to use.  Defaults to 6.
       nSwap -- the index of the swap-point server.  Defaults to nHops/2.
       startAt/endAt -- A time range during which all servers must be valid.

       Paths are ordinarily comma-separated lists of server nicknames or
       server descriptor filenames, as in:
             'foo,bar,./descriptors/baz,quux'.

       You can use a colon as a separator to divides the first leg of the path
       from the second:
             'foo,bar:baz,quux'.
       If nSwap and a colon are both used, they must match, or MixError is
       raised.

       You can use a star to specify a fill point where randomly-selected
       servers will be added:
             'foo,bar,*,quux'.

       The nHops argument must be consistent with the path, if both are
       specified.  Specifically, if nHops is used _without_ a star on the
       path, nHops must equal the path length; and if nHops is used _with_ a
       star on the path, nHops must be >= the path length.

       DOCDOC halfpath, address=None, defaultNHops
    """
    if not path:
        path = '*'

    # Turn 'path' into a list of server names, '*', and '*swap*'.
    #  (* is not a valid nickname character, so we're safe.)
    path = path.replace(":", ",*swap*,").split(",")
    # Strip whitespace around the commas and colon.
    path = [ s.strip() for s in path ]
    # List of servers that appear on the path before the '*'
    enterPath = []
    # List of servers that appear after the '*'.
    exitPath = []
    # Path we're currently appending to.
    cur = enterPath
    # Positions of '*' and ':" within the path, if we've seen them yet.
    starPos = swapPos = None
    # Walk over the path
    for idx in xrange(len(path)):
        ent = path[idx]
        if ent == "*":
            if starPos is not None:
                raise MixError("Can't have two wildcards in a path")
            starPos = idx
            cur = exitPath
        elif ent == "*swap*":
            if swapPos is not None:
                raise MixError("Can't specify swap point twice")
            swapPos = idx
        else:
            cur.append(ent)

    # Now, we set the variables myNHops and myNSwap to the values of
    # nHops and nSwap (if any) implicit in the parsed path.
    if starPos is None:
        myNHops = len(enterPath)
    else:
        if nHops:
            myNHops = nHops
        elif defaultNHops is not None:
            myNHops = defaultNHops
        else:
            myNHops = 6

    if swapPos is None:
        # a,b,c,d or a,b,*,c
        myNSwap = None
    elif starPos is None or swapPos < starPos:
        # a,b:c,d or a,b:c,*,d
        myNSwap = swapPos - 1
    else:
        # a,*,b:c,d
        # There are len(path)-swapPos-1 servers after the swap point.
        # There are a total of myNHops servers.
        # Thus, there are myNHops-(len(path)-swapPos-1) servers up to and
        #  including the swap server.
        # So, the swap server is at index myNHops - (len(path)-swapPos-1) -1,
        #   which is the same as...
        myNSwap = myNHops - len(path) + swapPos
        # But we need to adjust for the last node that we may have to
        #   add because of the address
        if address.getRouting()[2]:
            myNSwap -= 1

    # Check myNSwap for consistency
    if nSwap is not None:
        if myNSwap is not None and myNSwap != nSwap:
            raise MixError("Mismatch between specified swap points")
        myNSwap = nSwap

    # Check myNHops for consistency
    if nHops is not None:
        if myNHops is not None and myNHops != nHops:
            raise MixError("Mismatch between specified number of hops")
        elif nHops < len(enterPath)+len(exitPath):
            raise MixError("Mismatch between specified number of hops")

        myNHops = nHops

    # Finally, resolve the path.
    return resolvePath(keystore, address, enterPath, exitPath,
                       myNHops, myNSwap, startAt, endAt, halfPath=halfPath)

def parsePathLeg(keystore, config, path, nHops, address=None,
                 startAt=None, endAt=None, defaultNHops=None):
    "DOCDOC"
    path1, path2 = parsePath(keystore, config, path, address, nHops, nSwap=-1,
                             startAt=startAt, endAt=endAt, halfPath=1,
                             defaultNHops=defaultNHops)
    assert path1 == []
    return path2
    
class ClientKeyring:
    "DOCDOC"
    #DOCDOC
    # XXXX003 testme
    def __init__(self, keyDir):
        self.keyDir = keyDir
        createPrivateDir(self.keyDir)
        self.surbKey = None

    def getSURBKey(self, create=0):
        """DOCDOC"""
        if self.surbKey is not None:
            return self.surbKey
        fn = os.path.join(self.keyDir, "SURBKey")
        self.surbKey = self._getKey(fn, magic="SURBKEY0", which="reply block",
                                    create=create)
        return self.surbKey

    def _getKey(self, fn, magic, which, bytes=20, create=0):
        if os.path.exists(fn):
            self._checkMagic(fn, magic)
            while 1:
                p = self._getPassword(which)
                try:
                    return self._load(fn, magic, p)
                except MixError, e:
                    LOG.error("Cannot load key: %s", e)
        elif create:
            LOG.warn("No %s key found; generating.", which)
            key = trng(bytes)
            p = self._getNewPassword(which)
            self._save(fn, key, magic, p)
            return key
        else:
            return None

    def _checkMagic(self, fn, magic):
        f = open(fn, 'rb')
        s = f.read()
        f.close()
        if not s.startswith(magic):
            raise MixError("Invalid magic on key file")

    def _save(self, fn, data, magic, password):
        # File holds magic, salt (8 bytes), enc(key,data+sha1(data+salt+magic))
        #      where key = sha1(salt+password+salt)[:16]
        salt = mixminion.Crypto.getCommonPRNG().getBytes(8)
        key = sha1(salt+password+salt)[:16]
        f = open(fn, 'wb')
        f.write(magic)
        f.write(salt)
        f.write(ctr_crypt(data+sha1(data+salt+magic), key))
        f.close()

    def _load(self, fn, magic, password):
        f = open(fn, 'rb')
        s = f.read()
        f.close()
        if not s.startswith(magic):
            raise MixError("Invalid key file")
        s = s[len(magic):]
        if len(s) < 8:
            raise MixError("Key file too short")
        salt = s[:8]
        s = s[8:]
        if len(s) < 20:
            raise MixError("Key file too short")
        key = sha1(salt+password+salt)[:16]
        s = ctr_crypt(s, key)
        data, hash = s[:-20], s[-20:]
        if hash != sha1(data+salt+magic):
            raise MixError("Incorrect password")
        return data

    def _getPassword(self, which):
        s = "Enter password for %s:"%which
        p = getpass.getpass(s)
        return p

    def _getNewPassword(self, which):
        s1 = "Enter new password for %s:"%which
        s2 = "Verify password:".rjust(len(s1))
        while 1:
            p1 = getpass.getpass(s1)
            p2 = getpass.getpass(s2)
            if p1 == p2:
                return p1
            print "Passwords do not match."

def installDefaultConfig(fname):
    """Create a default, 'fail-safe' configuration in a given file"""
    LOG.warn("No configuration file found. Installing default file in %s",
                  fname)
    f = open(os.path.expanduser(fname), 'w')
    f.write("""\
# This file contains your options for the mixminion client.
[Host]
## Use this option to specify a 'secure remove' command.
#ShredCommand: rm -f
## Use this option to specify a nonstandard entropy source.
#EntropySource: /dev/urandom

[DirectoryServers]
# Not yet implemented

[User]
## By default, mixminion puts your files in ~/.mixminion.  You can override
## this directory here.
#UserDir: ~/.mixminion

[Security]
##DOCDOC
PathLength: 4
#SURBAddress: <your address here>
#SURBPathLength: 3 DOCDOC
#SURBLifetime: 7 days DOCDOC

[Network]
ConnectionTimeout: 20 seconds

""")
    f.close()

class SURBLog:
    """DOCDOC"""
    #DOCDOC
    # XXXX003 testme
    
    # DB holds HEX(hash) -> str(expiry)
    def __init__(self, filename, forceClean=0):
        parent, shortfn = os.path.split(filename)
        createPrivateDir(parent)
        LOG.debug("Opening SURB log")
        # DOCDOC MUST HOLD LOCK WHILE OPEN
        self.log = anydbm.open(filename, 'c')
        lastCleaned = int(self.log['LAST_CLEANED'])
        if lastCleaned < time.time()-24*60*60 or forceClean:
            self.clean()

    def close(self):
        """DOCDOC"""
        self.log.close()

    def isSURBUsed(self, surb):
        hash = binascii.b2a_hex(sha1(surb.pack()))
        try:
            _ = self.log[hash]
            return 1
        except KeyError:
            return 0

    def markSURBUsed(self, surb):
        hash = binascii.b2a_hex(sha1(surb.pack()))
        self.log[hash] = str(surb.timestamp)

    def clean(self, now=None):
        if now is None:
            now = time.time() + 60*60
        allHashes = self.log.keys()
        removed = []
        for hash in allHashes:
            if self.log[hash] < now:
                removed.append(hash)
        del allHashes
        for hash in removed:
            del self.log[hash]
        self.log['LAST_CLEANED'] = str(now)

class ClientPool:
    "DOCDOC"
    ## DOCDOC
    # XXXX003 testme

    def __init__(self, directory, prng=None):
        self.dir = directory
        createPrivateDir(directory)
        if prng is not None:
            self.prng = prng
        else:
            self.prng = mixminion.Crypto.getCommonPRNG()

    def poolPacket(self, message, firstHop):
        clientLock()
        f, handle = self.prng.openNewFile(self.dir, "pkt_", 1)
        cPickle.dump(("PACKET-0", message, firstHop,
                      previousMidnight(time.time())), f, 1)
        f.close()
        return handle
    
    def getHandles(self):
        clientLock()
        fnames = os.listdir(self.dir)
        handles = []
        for fname in fnames:
            if fname.startswith("pkt_"):
                handles.append(fname[4:])
        return handles

    def getPacket(self, handle):
        f = open(os.path.join(self.dir, "pkt_"+handle), 'rb')
        magic, message, firstHop, when = cPickle.load(f)
        f.close()
        if magic != "PACKET-0":
            LOG.error("Unrecognized packet format for %s",handle)
            return None
        return message, firstHop, when

    def packetExists(self, handle):
        fname = os.path.join(self.dir, "pkt_"+handle)
        return os.path.exists(fname)
        
    def removePacket(self, handle):
        fname = os.path.join(self.dir, "pkt_"+handle)
        secureDelete(fname, blocking=1)

    def inspectPool(self, now=None):
        if now is None:
            now = time.time()
        handles = self.getHandles()
        timesByServer = {}
        for h in handles:
            _, routing, when = self.getPacket(h)
            timesByServer.setdefault(routing, []).append(when)
        for s in timesByServer.keys():
            count = len(timesByServer[s])
            oldest = min(timesByServer[s])
            days = floorDiv(now - oldest, 24*60*60)
            if days < 1:
                days = "<1"
            print "%2d messages for server at %s:%s (oldest is %s days old)"%(
                count, s.ip, s.port, days)

class MixminionClient:
    """Access point for client functionality.  Currently, this is limited
       to generating and sending forward messages"""
    ## Fields:
    # config: The ClientConfig object with the current configuration
    # prng: A pseudo-random number generator for padding and path selection
    # keyDir: DOCDOC
    # surbKey: DOCDOC
    # pool: DOCDOC
    def __init__(self, conf):
        """Create a new MixminionClient with a given configuration"""
        self.config = conf

        # Make directories
        userdir = os.path.expanduser(self.config['User']['UserDir'])
        createPrivateDir(userdir)
        keyDir = os.path.join(userdir, "keys")
        self.keys = ClientKeyring(keyDir)
        self.surbLogFilename = os.path.join("userdir", "surbs", "log")

        # Initialize PRNG
        self.prng = mixminion.Crypto.getCommonPRNG()
        self.pool = ClientPool(os.path.join(userdir, "pool"))

    def sendForwardMessage(self, address, payload, servers1, servers2,
                           forcePool=0, forceNoPool=0):
        """Generate and send a forward message.
            address -- the results of a parseAddress call
            payload -- the contents of the message to send
            path1,path2 -- lists of servers for the first and second legs of
               the path, respectively.
            DOCDOC pool options"""

        message, firstHop = \
                 self.generateForwardMessage(address, payload,
                                             servers1, servers2)

        routing = firstHop.getRoutingInfo()

        if forcePool:
            self.poolMessages([message], routing)
        else:
            self.sendMessages([message], routing, noPool=forceNoPool)

    def sendReplyMessage(self, payload, servers, surbList, forcePool=0,
                         forceNoPool=0):
        """
        DOCDOC pool options
        """
        #XXXX003 testme
        message, firstHop = \
                 self.generateReplyMessage(payload, servers, surbList)

        routing = firstHop.getRoutingInfo()
        
        if forcePool:
            self.poolMessages([message], routing)
        else:
            self.sendMessages([message], routing, noPool=forceNoPool)


    def generateReplyBlock(self, address, servers, expiryTime=0):
        """
        DOCDOC
        """
        #XXXX003 testme
        key = self.keys.getSURBKey(create=1)
        exitType, exitInfo, _ = address.getRouting()

        block = mixminion.BuildMessage.buildReplyBlock(
            servers, exitType, exitInfo, key, expiryTime)

        return block

    def generateForwardMessage(self, address, payload, servers1, servers2):
        """Generate a forward message, but do not send it.  Returns
           a tuple of (the message body, a ServerInfo for the first hop.)

            address -- the results of a parseAddress call
            payload -- the contents of the message to send  (None for DROP
              messages)
            path1,path2 -- lists of servers.
            """

        #XXXX003 testme
        routingType, routingInfo, _ = address.getRouting()
        LOG.info("Generating payload...")
        msg = mixminion.BuildMessage.buildForwardMessage(
            payload, routingType, routingInfo, servers1, servers2,
            self.prng)
        return msg, servers1[0]

    def generateReplyMessage(self, payload, servers, surbList, now=None):
        """
        DOCDOC
        """
        #XXXX003 testme
        if now is None:
            now = time.time()
        clientLock()
        surbLog = SURBLog(self.surbLogFilename)
        try:
            for surb in surbList:
                expiry = surb.timestamp
                timeLeft = expiry - now
                if surbLog.isSURBUsed(surb):
                    LOG.warn("Skipping used reply block")
                    continue
                elif timeLeft < 60:
                    LOG.warn("Skipping expired reply (expired at %s)",
                             formatTime(expiry, 1))
                    continue
                elif timeLeft < 3*60*30:
                    LOG.warn("Reply block will expire in %s hours, %s minutes",
                             floorDiv(timeLeft, 60), int(timeLeft % 60))
                    continue
            
                LOG.info("Generating payload...")
                msg = mixminion.BuildMessage.buildReplyMessage(
                    payload, servers, surb, self.prng)

                surbLog.markSURBUsed(surb)
                return msg, servers[0]
            raise MixError("No usable SURBs found.")
        finally:
            surbLog.close()
            clientUnlock()

    def sendMessages(self, msgList, routingInfo, noPool=0, lazyPool=0,
                     warnIfLost=1):
        """Given a list of packets and a ServerInfo object, sends the
           packets to the server via MMTP

           DOCDOC ServerInfo or IPV4Info...
           """
        #XXXX003 testme
        LOG.info("Connecting...")
        timeout = self.config['Network'].get('ConnectionTimeout')
        if timeout:
            timeout = timeout[2]

        if noPool or lazyPool: 
            handles = []
        else:
            handles = self.poolMessages(msgList, routingInfo)

        try:
            try:
                # May raise TimeoutError
                mixminion.MMTPClient.sendMessages(routingInfo,
                                                  msgList,
                                                  timeout)
            except:
                if noPool and warnIfLost:
                    LOG.error("Error with pooling disabled: message lost")
                elif lazyPool:
                    LOG.info("Error while delivering message; message pooled")
                    self.poolMessages(msgList, routingInfo)
                else:
                    LOG.info("Error while delivering message; leaving in pool")
                raise
            try:
                clientLock()
                for h in handles:
                    if self.pool.packetExists(h):
                        self.pool.removePacket(h)
            finally:
                clientUnlock()
        except MixProtocolError, e:
            raise UIError(str(e))
            
    def flushPool(self):
        """
        DOCDOC pool options
        """
        #XXXX003 testme
        LOG.info("Flushing message pool")
        # XXXX This is inefficient in space!
        clientLock()
        try:
            handles = self.pool.getHandles()
            LOG.info("Found %s pending messages", len(handles))
            messagesByServer = {}
            for h in handles:
                message, routing, _ = self.pool.getPacket(h)
                messagesByServer.setdefault(routing, []).append((message, h))
        finally:
            clientUnlock()
            
        for routing in messagesByServer.keys():
            LOG.info("Sending %s messages to %s:%s...",
                     len(messagesByServer[routing]), routing.ip, routing.port)
            msgs = [ m for m, _ in messagesByServer[routing] ]
            handles = [ h for _, h in messagesByServer[routing] ] 
            try:
                self.sendMessages(msgs, routing, noPool=1, warnIfLost=0)
                LOG.info("... messages sent.")
                try:
                    clientLock()
                    for h in handles:
                        if self.pool.packetExists(h):
                            self.pool.removePacket(h)
                finally:
                    clientUnlock()
            except MixError:
                LOG.error("Can't deliver messages to %s:%s; leaving in pool",
                          routing.ip, routing.port)
        LOG.info("Pool flushed")

    def poolMessages(self, msgList, routing):
        """
        DOCDOC
        """
        #XXXX003 testme
        LOG.trace("Pooling messages")
        handles = []
        try:
            clientLock()
            for msg in msgList:
                h = self.pool.poolPacket(msg, routing)
                handles.append(h)
        finally:
            clientUnlock()
        if len(msgList) > 1:
            LOG.info("Messages pooled")
        else:
            LOG.info("Message pooled")
        return handles

    def decodeMessage(self, s, force=0):
        """DOCDOC

           Raises ParseError
        """
        #XXXX003 testme
        results = []
        idx = 0
        while idx < len(s):
            msg, idx = parseTextEncodedMessage(s, idx=idx, force=force)
            if msg is None:
                return results
            if msg.isOvercompressed() and not force:
                LOG.warn("Message is a possible zlib bomb; not uncompressing")
            if not msg.isEncrypted():
                results.append(msg.getContents())
            else:
                surbKey = self.keys.getSURBKey(create=0)
                results.append(
                    mixminion.BuildMessage.decodePayload(msg.getContents(),
                                                         tag=msg.getTag(),
                                                         userKey=surbKey))
        return results

def parseAddress(s):
    """Parse and validate an address; takes a string, and returns an Address
       object.

       Accepts strings of the format:
              mbox:<mailboxname>@<server>
           OR smtp:<email address>
           OR <email address> (smtp is implicit)
           OR drop
           OR 0x<routing type>:<routing info>
    """
    # ???? Should this should get refactored into clientmodules, or someplace?
    if s.lower() == 'drop':
        return Address(DROP_TYPE, "", None)
    elif s.lower() == 'test':
        return Address(0xFFFE, "", None)
    elif ':' not in s:
        if isSMTPMailbox(s):
            return Address(SMTP_TYPE, s, None)
        else:
            raise ParseError("Can't parse address %s"%s)
    tp,val = s.split(':', 1)
    tp = tp.lower()
    if tp.startswith("0x"):
        try:
            tp = int(tp[2:], 16)
        except ValueError:
            raise ParseError("Invalid hexidecimal value %s"%tp)
        if not (0x0000 <= tp <= 0xFFFF):
            raise ParseError("Invalid type: 0x%04x"%tp)
        return Address(tp, val, None)
    elif tp == 'mbox':
        if "@" in val:
            mbox, server = val.split("@",1)
            return Address(MBOX_TYPE, parseMBOXInfo(mbox).pack(), server)
        else:
            return Address(MBOX_TYPE, parseMBOXInfo(val).pack(), None)
    elif tp == 'smtp':
        # May raise ParseError
        return Address(SMTP_TYPE, parseSMTPInfo(val).pack(), None)
    elif tp == 'test':
        return Address(0xFFFE, val, None)
    else:
        raise ParseError("Unrecognized address type: %s"%s)

class Address:
    """Represents the target address for a Mixminion message.
       Consists of the exitType for the final hop, the routingInfo for
       the last hop, and (optionally) a server to use as the last hop.
       """
    def __init__(self, exitType, exitAddress, lastHop=None):
        self.exitType = exitType
        self.exitAddress = exitAddress
        self.lastHop = lastHop
    def getRouting(self):
        return self.exitType, self.exitAddress, self.lastHop

def readConfigFile(configFile):
    """Given a configuration file (possibly none) as specified on the command
       line, return a ClientConfig object.

       Tries to look for the configuration file in the following places:
          - as specified on the command line,
          - as specifed in $MIXMINIONRC
          - in ~/.mixminionrc.

       If the configuration file is not found in the specified location,
       we create a fresh one.
    """
    if configFile is None:
        configFile = os.environ.get("MIXMINIONRC", None)
    if configFile is None:
        configFile = "~/.mixminionrc"
    configFile = os.path.expanduser(configFile)

    if not os.path.exists(configFile):
        installDefaultConfig(configFile)

    try:
        return ClientConfig(fname=configFile)
    except (IOError, OSError), e:
        print >>sys.stderr, "Error reading configuration file %r:"%configFile
        print >>sys.stderr, "   ", str(e)
        sys.exit(1)
    except ConfigError, e:
        print >>sys.stderr, "Error in configuration file %r"%configFile
        print >>sys.stderr, str(e)
        sys.exit(1)
    return None #suppress pychecker warning

class CLIArgumentParser:
    "DOCDOC"
    def __init__(self, opts,
                 wantConfig=0, wantKeystore=0, wantClient=0, wantLog=0,
                 wantDownload=0, wantForwardPath=0, wantReplyPath=0,
                 minHops=0):
        """DOCDOC"""
        self.config = None
        self.keystore = None
        self.client = None
        self.keyring = None
        self.path1 = None
        self.path2 = None

        if wantForwardPath: wantKeystore = 1
        if wantReplyPath: wantKeystore = 1
        if wantDownload: wantKeystore = 1
        if wantKeystore: wantConfig = 1
        if wantClient: wantConfig = 1

        self.wantConfig = wantConfig
        self.wantKeystore = wantKeystore
        self.wantClient = wantClient
        self.wantLog = wantLog
        self.wantDownload = wantDownload
        self.wantForwardPath = wantForwardPath
        self.wantReplyPath = wantReplyPath
        
        self.configFile = None
        self.verbose = 0
        self.download = None

        self.path = None
        self.nHops = None
        self.path = None
        self.swapAt = None
        self.address = None
        self.lifetime = None
        self.replyBlock = None

        self.forcePool = None
        self.forceNoPool = None

        for o,v in opts:
            if o in ('-h', '--help'):
                raise UsageError()
            elif o in ('-f', '--config'):
                self.configFile = v
            elif o in ('-v', '--verbose'):
                self.verbose = 1
            elif o in ('-D', '--download-directory'):
                assert wantDownload
                download = v.lower()
                if download in ('0','no','false','n','f'):
                    self.download = 0
                elif download in ('1','yes','true','y','t','force'):
                    self.download = 1
                else:
                    raise UsageError(
                        "Unrecognized value for %s. Expected 'yes' or 'no'"%o)
            elif o in ('-t', '--to'):
                assert wantForwardPath or wantReplyPath
                try:
                    self.address = parseAddress(v)
                except ParseError, e:
                    raise UsageError(str(e))
            elif o in ('-R', '--reply-block'):
                assert wantForwardPath
                self.replyBlock = v
            elif o == '--swap-at':
                assert wantForwardPath
                try:
                    self.swapAt = int(v)-1
                except ValueError:
                    raise UsageError("%s expects an integer"%o)
            elif o in ('-H', '--hops'):
                assert wantForwardPath or wantReplyPath
                try:
                    self.nHops = int(v)
                    if minHops and self.nHops < minHops:
                        raise UsageError("Must have at least 2 hops")
                except ValueError:
                    raise UsageError("%s expects an integer"%o)
            elif o in ('-P', '--path'):
                assert wantForwardPath or wantReplyPath
                self.path = v
            elif o in ('--lifetime',):
                assert wantReplyPath
                try:
                    self.lifetime = int(v)
                except ValueError:
                    raise UsageError("%s expects an integer"%o)
            elif o in ('--pool',):
                self.forcePool = 1
            elif o in ('--no-pool',):
                self.forceNoPool = 1

    def init(self):
        """DOCDOC"""
        if self.wantConfig:
            self.config = readConfigFile(self.configFile)
            if self.wantLog:
                LOG.configure(self.config)
                if self.verbose:
                    LOG.setMinSeverity("TRACE")
                else:
                    LOG.setMinSeverity("INFO")
            mixminion.Common.configureShredCommand(self.config)
            if not self.verbose:
                try:
                    LOG.setMinSeverity("WARN")
                    mixminion.Crypto.init_crypto(self.config)
                finally:
                    LOG.setMinSeverity("INFO")
            else:
                mixminion.Crypto.init_crypto(self.config)
                
            userdir = os.path.expanduser(self.config['User']['UserDir'])
            configureClientLock(os.path.join(userdir, "lock"))
        else:
            if self.wantLog:
                LOG.setMinSeverity("ERROR")
            userdir = None
            
        if self.wantClient:
            assert self.wantConfig
            LOG.debug("Configuring client")
            self.client = MixminionClient(self.config)

        if self.wantKeystore:
            assert self.wantConfig
            LOG.debug("Configuring server list")
            self.keystore = ClientDirectory(userdir)

        if self.wantDownload:
            assert self.wantKeystore
            if self.download != 0:
                try:
                    clientLock()
                    self.keystore.updateDirectory(forceDownload=self.download)
                finally:
                    clientUnlock()

    def parsePath(self):
        """DOCDOC"""
        if self.wantReplyPath and self.address is None:
            address = self.config['Security'].get('SURBAddress')
            if address is None:
                raise UsageError("No recipient specified; exiting.")
            try:
                self.address = parseAddress(address)
            except ParseError, e:
                raise UsageError(str(e))
        elif self.address is None and self.replyBlock is None:
            raise UsageError("No recipients specified; exiting")
        elif self.address is not None and self.replyBlock is not None:
            raise UsageError("Cannot use both a recipient and a reply block")
        elif self.replyBlock is not None:
            useRB = 1
            f = open(self.replyBlock, 'rb')
            s = f.read()
            f.close()
            try:
                if stringContains(s, "== BEGIN TYPE III REPLY BLOCK =="):
                    surbs = parseTextReplyBlocks(s)
                else:
                    surbs = parseReplyBlocks(s)
            except ParseError, e:
                raise UIError("Error parsing %s: %s" % (self.replyBlock, e))
        else:
            assert self.address is not None
            useRB = 0

        if self.wantReplyPath:
            if self.lifetime is not None:
                duration = self.lifetime * 24*60*60
            else:
                duration = self.config['Security']['SURBLifetime'][2]
                
            self.endTime = succeedingMidnight(time.time() + duration)

            defHops = self.config['Security'].get("SURBPathLength", 4)
            self.path1 = parsePathLeg(self.keystore, self.config, self.path,
                                      self.nHops, self.address,
                                      startAt=time.time(),
                                      endAt=self.endTime,
                                      defaultNHops=defHops)
            self.path2 = None
            LOG.info("Selected path is %s",
                     ",".join([ s.getNickname() for s in self.path1 ]))
        elif useRB:
            assert self.wantForwardPath
            defHops = self.config['Security'].get("PathLength", 6)
            self.path1 = parsePathLeg(self.keystore, self.config, self.path,
                                      self.nHops, defaultNHops=defHops)
            self.path2 = surbs
            self.usingSURBList = 1
            LOG.info("Selected path is %s:<reply block>",
                     ",".join([ s.getNickname() for s in self.path1 ]))
        else:
            assert self.wantForwardPath
            defHops = self.config['Security'].get("PathLength", 6)
            self.path1, self.path2 = \
                        parsePath(self.keystore, self.config, self.path,
                                  self.address, self.nHops, self.swapAt,
                                  defaultNHops=defHops)
            self.usingSURBList = 0
            LOG.info("Selected path is %s:%s",
                     ",".join([ s.getNickname() for s in self.path1 ]),
                     ",".join([ s.getNickname() for s in self.path2 ]))

    def getForwardPath(self):
        """DOCDOC"""
        return self.path1, self.path2
    
    def getReplyPath(self):
        """DOCDOC"""
        return self.path1
    
_SEND_USAGE = """\
Usage: %(cmd)s [options]
        <-t address>|<--to=address>|<-R reply-block>|--reply-block=reply-block>
Options:
  -h, --help                 Print this usage message and exit.
  -v, --verbose              Display extra debugging messages.
  -D <yes|no>, --download-directory=<yes|no>
                             Force the client to download/not to download a
                               fresh directory.
  -f <file>, --config=<file> Use a configuration file other than ~.mixminionrc
                               (You can also use MIXMINIONRC=FILE)
  -H <n>, --hops=<n>         Force the path to use <n> hops.
  -i <file>, --input=<file>  Read the message to send from <file>.
                               (Defaults to standard input.)
  -P <path>, --path=<path>   Specify an explicit message path.
  -t address, --to=address   Specify the recipient's address.
  --swap-at=<n>              Spcecify an explicit swap point.

EXAMPLES:
  Send a message contained in a file <data> to user@domain.
      %(cmd)s -t user@domain -i data
  As above, but force 6 hops.
      %(cmd)s -t user@domain -i data -H 6
  As above, but use the server nicknamed Foo for the first hop and the server
  whose descriptor is stored in bar/baz for the last hop.
      %(cmd)s -t user@domain -i data -H 6 -P 'Foo,*,bar/baz'
  As above, but switch legs of the path after the second hop.
      %(cmd)s -t user@domain -i data -H 6 -P 'Foo,*,bar/baz' --swap-at=2
  Specify an explicit path
      %(cmd)s -t user@domain -i data -P 'Foo,Bar,Baz,Quux,Fee,Fie,Foe'
  Specify an explicit path with a swap point
      %(cmd)s -t user@domain -i data -P 'Foo,Bar,Baz,Quux:Fee,Fie,Foe'
  Read the message from standard input.
      %(cmd)s -t user@domain
  Force a fresh directory download
      %(cmd)s -D yes
  Send a message without dowloading a new directory, even if the current
  directory is out of date.
      %(cmd)s -D no -t user@domain -i data

DOCDOC reply block
""".strip()

def usageAndExit(cmd, error=None):
    if error:
        print >>sys.stderr, "ERROR: %s"%error
        print >>sys.stderr, "For usage, run 'mixminion send --help'"
        sys.exit(1)
    print _SEND_USAGE % { 'cmd' : "mixminion send" }
    sys.exit(0)

# NOTE: This isn't the final client interface.  Many or all options will
#     change between now and 1.0.0
def runClient(cmd, args):
    #DOCDOC Comment this message    
    if cmd.endswith(" client"):
        print "The 'client' command is deprecated.  Use 'send' instead."
    poolMode = 0
    if cmd.endswith(" pool"):
        poolMode = 1

    options, args = getopt.getopt(args, "hvf:D:t:H:P:R:i:",
             ["help", "verbose", "config=", "download-directory=",
              "to=", "hops=", "swap-at=", "path=", "reply-block=",
              "input=", "pool", "no-pool" ])
              
    if not options:
        usageAndExit(cmd)
    
    inFile = None
    for opt,val in options:
        if opt in ('-i', '--input'):
            inFile = val

    if args:
        usageAndExit(cmd,"Unexpected arguments")

    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantKeystore=1,
                                   wantClient=1, wantLog=1, wantDownload=1,
                                   wantForwardPath=1)
        if poolMode and parser.forceNoPool:
            raise UsageError("Can't use --no-pool option with pool command")
        if parser.forcePool and parser.forceNoPool:
            raise UsageError("Can't use both --pool and --no-pool")
    except UIError, e:
        e.dump()
        usageAndExit(cmd)

    # FFFF Make pooling configurable from .mixminionrc
    forcePool = poolMode or parser.forcePool
    forceNoPool = parser.forceNoPool

    parser.init()
    client = parser.client

    try:
        parser.parsePath()
    except UIError, e:
        e.dump()
        sys.exit(1)

    path1, path2 = parser.getForwardPath()
    address = parser.address

    # XXXX Clean up this ugly control structure.
    if address and inFile is None and address.getRouting()[0] == DROP_TYPE:
        payload = None
        LOG.info("Sending dummy message")
    else:
        if address and address.getRouting()[0] == DROP_TYPE:
            LOG.error("Cannot send a payload with a DROP message.")
            sys.exit(0)

        if inFile is None:
            inFile = "-"

        if inFile == '-':
            f = sys.stdin
            print "Enter your message now.  Type Ctrl-D when you are done."
        else:
            f = open(inFile, 'r')

        try:
            payload = f.read()
            f.close()
        except KeyboardInterrupt:
            print "Interrupted.  Message not sent."
            sys.exit(1)

    try:
        if parser.usingSURBList:
            assert isinstance(path2, ListType)
            client.sendReplyMessage(payload, path1, path2,
                                    forcePool, forceNoPool)
        else:
            client.sendForwardMessage(address, payload, path1, path2,
                                      forcePool, forceNoPool)
    except UIError, e:
        e.dump()

_IMPORT_SERVER_USAGE = """\
Usage: %s [options] <filename> ...
Options:
   -h, --help:             Print this usage message and exit.
   -v, --verbose              Display extra debugging messages.
   -f FILE, --config=FILE  Use a configuration file other than ~/.mixminionrc
""".strip()

def importServer(cmd, args):
    options, args = getopt.getopt(args, "hf:v", ['help', 'config=', 'verbose'])

    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantKeystore=1,
                                   wantLog=1)
    except UIError, e:
        e.dump()
        print _IMPORT_SERVER_USAGE %cmd
        sys.exit(1)

    parser.init()
    keystore = parser.keystore

    try:
        clientLock()
        for filename in args:
            print "Importing from", filename
            try:
                keystore.importFromFile(filename)
            except MixError, e:
                print "Error while importing: %s" % e
    finally:
        clientUnlock()
        
    print "Done."

_LIST_SERVERS_USAGE = """\
Usage: %s [options]
Options:
  -h, --help:                Print this usage message and exit.
  -v, --verbose              Display extra debugging messages.
  -f <file>, --config=<file> Use a configuration file other than ~/.mixminionrc
                             (You can also use MIXMINIONRC=FILE)
  -D <yes|no>, --download-directory=<yes|no>
                             Force the client to download/not to download a
                               fresh directory.
""".strip()

def listServers(cmd, args):
    options, args = getopt.getopt(args, "hf:D:v",
                                  ['help', 'config=', "download-directory=",
                                   'verbose'])
    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantKeystore=1,
                                   wantLog=1, wantDownload=1)
    except UIError, e:
        e.dump()
        print _LIST_SERVERS_USAGE % cmd
        sys.exit(1)

    parser.init()
    keystore = parser.keystore

    for line in keystore.listServers():
        print line

_UPDATE_SERVERS_USAGE = """\
Usage: %s [options]
Options:
  -h, --help:                Print this usage message and exit.
  -v, --verbose              Display extra debugging messages.
  -f <file>, --config=<file> Use a configuration file other than ~/.mixminionrc
                             (You can also use MIXMINIONRC=FILE)
""".strip()

def updateServers(cmd, args):
    options, args = getopt.getopt(args, "hvf:", ['help', 'verbose', 'config='])
    
    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantKeystore=1,
                                   wantLog=1)
    except UIError, e:
        e.dump()
        print _UPDATE_SERVERS_USAGE % cmd
        sys.exit(1)

    parser.init()
    keystore = parser.keystore
    try:
        clientLock()
        keystore.updateDirectory(forceDownload=1)
    finally:
        clientUnlock()
    print "Directory updated"

_CLIENT_DECODE_USAGE = """\
Usage: %s [options] -i <file>|--input=<file>
Options:
  -h, --help:                Print this usage message and exit.
  -v, --verbose              Display extra debugging messages.
  -f <file>, --config=<file> Use a configuration file other than ~/.mixminionrc
                             (You can also use MIXMINIONRC=FILE)
  -F, --force:               Decode the input files, even if they seem
                             overcompressed.
  -o <file>, --output=<file> Write the results to <file> rather than stdout.
  
""".strip()

def clientDecode(cmd, args):
    #DOCDOC Comment me
    options, args = getopt.getopt(args, "hvf:o:Fi:",
          ['help', 'verbose', 'config=',
           'output=', 'force', 'input='])
           
    outputFile = '-'
    inputFile = None
    force = 0
    for o,v in options:
        if o in ('-o', '--output'):
            outputFile = v
        elif o in ('-F', '--force'):
            force = 1
        elif o in ('-i', '--input'):
            inputFile = v

    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantClient=1,
                                   wantLog=1)
    except UIError, e:
        e.dump()
        print _CLIENT_DECODE_USAGE %cmd
        sys.exit(1)

    if not inputFile:
        print >> sys.stderr, "Error: No input file specified"
        sys.exit(1)

    parser.init()
    client = parser.client
        
    if outputFile == '-':
        out = sys.stdout
    else:
        # ????003 Should we sometimes open this in text mode?
        out = open(outputFile, 'wb')

    if inputFile == '-':
        s = sys.stdin.read()
    else:
        try:
            f = open(inputFile, 'r')
            s = f.read()
            f.close()
        except OSError, e:
            LOG.error("Could not read file %s: %s", inputFile, e)
    try:
        res = client.decodeMessage(s, force=force)
    except ParseError, e:
        print "Couldn't parse message: %s"%e
        out.close()
        sys.exit(1)
        
    for r in res:
        out.write(r)
    out.close()

_GENERATE_SURB_USAGE = """\
Usage: %s [options]
  This space is temporarily left blank.
  DOCDOC
"""
def generateSURB(cmd, args):
    #DOCDOC Comment me
    options, args = getopt.getopt(args, "hvf:D:t:H:P:o:bn:",
          ['help', 'verbose', 'config=', 'download-directory=',
           'to=', 'hops=', 'path=', 'lifetime=',
           'output=', 'binary', 'count='])
           
    outputFile = '-'
    binary = 0
    count = 1
    for o,v in options:
        if o in ('-o', '--output'):
            outputFile = v
        elif o in ('-b', '--binary'):
            binary = 1
        elif o in ('-n', '--count'):
            try:
                count = int(v)
            except ValueError:
                print "ERROR: %s expects an integer" % o
                sys.exit(1)
            
    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantClient=1,
                                   wantLog=1, wantKeystore=1, wantDownload=1,
                                   wantReplyPath=1)
    except UIError, e:
        e.dump()
        print _GENERATE_SURB_USAGE % cmd
        sys.exit(1)

    if args:
        print >>sys.stderr, "Unexpected arguments"
        sys.exit(1)

    parser.init()
        
    client = parser.client

    try:
        parser.parsePath()
    except UIError, e:
        e.dump()
        sys.exit(1)
    
    path1 = parser.getReplyPath()
    address = parser.address

    if outputFile == '-':
        out = sys.stdout
    elif binary:
        out = open(outputFile, 'wb')
    else:
        out = open(outputFile, 'w')

    for i in xrange(count):
        surb = client.generateReplyBlock(address, path1, parser.endTime)
        if binary:
            out.write(surb.pack())
        else:
            out.write(surb.packAsText())
        if i != count-1:
            parser.parsePath()
            path1 = parser.getReplyPath()
          
    out.close()

_INSPECT_SURBS_USAGE = """\
Usage: %s [options] <files>
  This space is temporarily left blank.
  DOCDOC
"""

def inspectSURBs(cmd, args):
    options, args = getopt.getopt(args, "hvf:",
             ["help", "verbose", "config=", ])

    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantLog=1)
    except UIError, e:
        e.dump()
        print _INSPECT_SURBS_USAGE % cmd
        sys.exit(1)

    parser.init()

    for fn in args:
        f = open(fn, 'rb')
        s = f.read()
        f.close()
        print "==== %s"%fn
        try:
            if stringContains(s, "== BEGIN TYPE III REPLY BLOCK =="):
                surbs = parseTextReplyBlocks(s)
            else:
                surbs = parseReplyBlocks(s)
        
            for surb in surbs:
                print surb.format()
        except ParseError, e:
            print "Error while parsing: %s"%e
        

_FLUSH_POOL_USAGE = """\
Usage: %s [options]
  This space is temporarily left blank.
  DOCDOC
"""

def flushPool(cmd, args):
    options, args = getopt.getopt(args, "hvf:",
             ["help", "verbose", "config=", ])
    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantLog=1,
                                   wantClient=1)
    except UIError, e:
        e.dump()
        print _FLUSH_POOL_USAGE % cmd
        sys.exit(1)

    parser.init()
    client = parser.client

    client.flushPool()


_LIST_POOL_USAGE = """\
Usage: %s [options]
  This space is temporarily left blank.
  DOCDOC
"""

def listPool(cmd, args):
    options, args = getopt.getopt(args, "hvf:",
             ["help", "verbose", "config=", ])
    try:
        parser = CLIArgumentParser(options, wantConfig=1, wantLog=1,
                                   wantClient=1)
    except UIError, e:
        e.dump()
        print _LIST_POOL_USAGE % cmd
        sys.exit(1)

    parser.init()
    client = parser.client
    try:
        clientLock()
        client.pool.inspectPool()
    finally:
        clientUnlock()

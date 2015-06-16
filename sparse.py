# sparse.py - allow sparse checkouts of the working directory
#
# Copyright 2014 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

"""allow sparse checkouts of the working directory
"""

from mercurial import util, cmdutil, extensions, context, dirstate, commands
from mercurial import localrepo, error
from mercurial import match as matchmod
from mercurial import merge as mergemod
from mercurial.node import nullid
from mercurial.i18n import _
import errno, os, re, collections

cmdtable = {}
command = cmdutil.command(cmdtable)
testedwith = 'internal'

def uisetup(ui):
    _setupupdates(ui)
    _setupcommit(ui)

def extsetup(ui):
    _setuplog(ui)
    _setupadd(ui)
    _setupdirstate(ui)
    # if hgwatchman is installed, tell it to use our hash function
    try:
        hgwatchman = extensions.find('hgwatchman')
        def _hashignore(orig, ignore):
            return _hashmatcher(ignore)
        extensions.wrapfunction(hgwatchman, '_hashignore', _hashignore)
    except KeyError:
        pass

def reposetup(ui, repo):
    if not util.safehasattr(repo, 'dirstate'):
        return

    _wraprepo(ui, repo)

def wrapfilecache(cls, propname, wrapper):
    """Wraps a filecache property. These can't be wrapped using the normal
    wrapfunction. This should eventually go into upstream Mercurial.
    """
    origcls = cls
    assert callable(wrapper)
    stack = [cls]
    while stack:
        cls = stack.pop()
        if propname in cls.__dict__:
            origfn = cls.__dict__[propname].func
            assert callable(origfn)
            def wrap(*args, **kwargs):
                return wrapper(origfn, *args, **kwargs)
            cls.__dict__[propname].func = wrap
            return
        # Reverse the bases, so we descend first parents first
        stack.extend(reversed(cls.__bases__))

    raise AttributeError(_("type '%s' has no property '%s'") % (origcls,
                         propname))

def replacefilecache(cls, propname, replacement):
    """Replace a filecache property with a new class. This allows changing the
    cache invalidation condition."""
    origcls = cls
    assert callable(replacement)
    while cls is not object:
        if propname in cls.__dict__:
            orig = cls.__dict__[propname]
            setattr(cls, propname, replacement(orig))
            break
        cls = cls.__bases__[0]

    if cls is object:
        raise AttributeError(_("type '%s' has no property '%s'") % (origcls,
                             propname))

def _setupupdates(ui):
    def _calculateupdates(orig, repo, wctx, mctx, pas, branchmerge, force,
                          partial, mergeancestor, followcopies):
        """Filter updates to only lay out files that match the sparse rules.
        """
        actions, diverge, renamedelete = orig(repo, wctx, mctx, pas,
            branchmerge, force, partial, mergeancestor, followcopies)

        if not util.safehasattr(repo, 'sparsematch'):
            return actions, diverge, renamedelete

        files = set()
        prunedactions = {}
        oldrevs = [pctx.rev() for pctx in wctx.parents()]
        oldsparsematch = repo.sparsematch(*oldrevs)

        if branchmerge:
            # If we're merging, use the wctx filter, since we're merging into
            # the wctx.
            sparsematch = repo.sparsematch(wctx.parents()[0].rev())
        else:
            # If we're updating, use the target context's filter, since we're
            # moving to the target context.
            sparsematch = repo.sparsematch(mctx.rev())

        temporaryfiles = []
        for file, action in actions.iteritems():
            type, args, msg = action
            files.add(file)
            if sparsematch(file):
                prunedactions[file] = action
            elif type == 'm':
                temporaryfiles.append(file)
                prunedactions[file] = action
            elif branchmerge:
                if type != 'k':
                    temporaryfiles.append(file)
                    prunedactions[file] = action
            elif file in wctx:
                prunedactions[file] = ('r', args, msg)

        if len(temporaryfiles) > 0:
            ui.status("temporarily included %d file(s) in the sparse checkout for "
                "merging\n" % len(temporaryfiles))
            repo.addtemporaryincludes(temporaryfiles)

            # Add the new files to the working copy so they can be merged, etc
            actions = []
            message = 'temporarily adding to sparse checkout'
            wctxmanifest = repo[None].manifest()
            for file in temporaryfiles:
                if file in wctxmanifest:
                    fctx = repo[None][file]
                    actions.append((file, (fctx.flags(),), message))

            typeactions = collections.defaultdict(list)
            typeactions['g'] = actions
            mergemod.applyupdates(repo, typeactions, repo[None], repo['.'], False)

            dirstate = repo.dirstate
            for file, flags, msg in actions:
                dirstate.normal(file)

        profiles = repo.getactiveprofiles()
        changedprofiles = profiles & files
        # If an active profile changed during the update, refresh the checkout.
        # Don't do this during a branch merge, since all incoming changes should
        # have been handled by the temporary includes above.
        if changedprofiles and not branchmerge:
            mf = mctx.manifest()
            for file in mf:
                if file not in files:
                    old = oldsparsematch(file)
                    new = sparsematch(file)
                    if not old and new:
                        flags = mf.flags(file)
                        prunedactions[file] = ('g', (flags,), '')
                    elif old and not new:
                        prunedactions[file] = ('r', [], '')

        return prunedactions, diverge, renamedelete

    extensions.wrapfunction(mergemod, 'calculateupdates', _calculateupdates)

    def _update(orig, repo, node, branchmerge, *args, **kwargs):
        results = orig(repo, node, branchmerge, *args, **kwargs)

        # If we're updating to a location, clean up any stale temporary includes
        # (ex: this happens during hg rebase --abort).
        if not branchmerge and util.safehasattr(repo, 'sparsematch'):
            repo.prunetemporaryincludes()
        return results

    extensions.wrapfunction(mergemod, 'update', _update)

def _setupcommit(ui):
    def _refreshoncommit(orig, self, node):
        """Refresh the checkout when commits touch .hgsparse
        """
        orig(self, node)
        repo = self._repo
        if util.safehasattr(repo, 'sparsematch'):
            ctx = repo[node]
            _, _, profiles = repo.getsparsepatterns(ctx.rev())
            if set(profiles) & set(ctx.files()):
                origstatus = repo.status()
                origsparsematch = repo.sparsematch()
                _refresh(repo.ui, repo, origstatus, origsparsematch, True)

            repo.prunetemporaryincludes()

    extensions.wrapfunction(context.committablectx, 'markcommitted',
        _refreshoncommit)

def _setuplog(ui):
    entry = commands.table['^log|history']
    entry[1].append(('', 'sparse', None,
        "limit to commits affecting the sparse checkout"))

    def _logrevs(orig, repo, opts):
        revs = orig(repo, opts)
        if opts.get('sparse'):
            sparsematch = repo.sparsematch()
            def ctxmatch(rev):
                ctx = repo[rev]
                return any(f for f in ctx.files() if sparsematch(f))
            revs = revs.filter(ctxmatch)
        return revs
    extensions.wrapfunction(cmdutil, '_logrevs', _logrevs)

def _setupadd(ui):
    entry = commands.table['^add']
    entry[1].append(('s', 'sparse', None,
                    'also include directories of added files in sparse config'))

    def _add(orig, ui, repo, *pats, **opts):
        if opts.get('sparse'):
            dirs = set()
            for pat in pats:
                dirname, basename = util.split(pat)
                dirs.add(dirname)
            _config(ui, repo, list(dirs), include=True)
        orig(ui, repo, *pats, **opts)

    extensions.wrapcommand(commands.table, 'add', _add)

def _setupdirstate(ui):
    """Modify the dirstate to prevent stat'ing excluded files,
    and to prevent modifications to files outside the checkout.
    """

    def _dirstate(orig, repo):
        dirstate = orig(repo)
        dirstate.repo = repo
        return dirstate
    wrapfilecache(localrepo.localrepository, 'dirstate', _dirstate)

    # The atrocity below is needed to wrap dirstate._ignore. It is a cached
    # property, which means normal function wrapping doesn't work.
    class ignorewrapper(object):
        def __init__(self, orig):
            self.orig = orig
            self.origignore = None
            self.func = None
            self.sparsematch = None

        def __get__(self, obj, type=None):
            repo = obj.repo
            sparsematch = repo.sparsematch()
            origignore = self.orig.__get__(obj)
            if self.sparsematch != sparsematch or self.origignore != origignore:
                self.func = unionmatcher([origignore, negatematcher(sparsematch)])
                self.sparsematch = sparsematch
                self.origignore = origignore
            return self.func

        def __set__(self, obj, value):
            return self.orig.__set__(obj, value)

        def __delete__(self, obj):
            return self.orig.__delete__(obj)

    replacefilecache(dirstate.dirstate, '_ignore', ignorewrapper)

    # dirstate.rebuild should not add non-matching files
    def _rebuild(orig, self, parent, allfiles, changedfiles=None):
        matcher = self.repo.sparsematch()
        allfiles = allfiles.matches(matcher)
        if changedfiles:
            changedfiles = [f for f in changedfiles if matcher(f)]
        return orig(self, parent, allfiles, changedfiles)
    extensions.wrapfunction(dirstate.dirstate, 'rebuild', _rebuild)

    # Prevent adding files that are outside the sparse checkout
    editfuncs = ['normal', 'add', 'normallookup', 'copy', 'remove', 'merge']
    hint = _('include file with `hg sparse --include <pattern>` or use ' +
             '`hg add -s <file>` to include file directory while adding')
    for func in editfuncs:
        def _wrapper(orig, self, *args):
            repo = self.repo
            dirstate = repo.dirstate
            sparsematch = repo.sparsematch()
            for f in args:
                if not sparsematch(f) and f not in dirstate:
                    raise util.Abort(_("cannot add '%s' - it is outside the " +
                                     "sparse checkout") % f, hint=hint)
            return orig(self, *args)
        extensions.wrapfunction(dirstate.dirstate, func, _wrapper)

def _wraprepo(ui, repo):
    class SparseRepo(repo.__class__):
        def readsparseconfig(self, raw):
            """Takes a string sparse config and returns the includes,
            excludes, and profiles it specified.
            """
            includes = set()
            excludes = set()
            current = includes
            profiles = []
            for line in raw.split('\n'):
                line = line.strip()
                if line.startswith('%include '):
                    line = line[9:].strip()
                    if line:
                        profiles.append(line)
                elif line == '[include]':
                    if current != includes:
                        raise util.abort(_('.hg/sparse cannot have includes ' +
                            'after excludes'))
                    continue
                elif line == '[exclude]':
                    current = excludes
                elif line:
                    current.add(line)

            return includes, excludes, profiles

        def getsparsepatterns(self, rev):
            """Returns the include/exclude patterns specified by the
            given rev.
            """
            if not self.opener.exists('sparse'):
                return set(), set(), []
            if rev is None:
                raise util.Abort(_("cannot parse sparse patterns from " +
                    "working copy"))

            raw = self.opener.read('sparse')
            includes, excludes, profiles = self.readsparseconfig(raw)

            ctx = self[rev]
            if profiles:
                visited = set()
                while profiles:
                    profile = profiles.pop()
                    if profile in visited:
                        continue
                    visited.add(profile)

                    try:
                        raw = self.getrawprofile(profile, rev)
                    except error.ManifestLookupError:
                        self.ui.debug("warning: sparse profile '%s' not found "
                            "in rev %s - ignoring it\n" % (profile, ctx))
                        continue
                    pincludes, pexcludes, subprofs = \
                        self.readsparseconfig(raw)
                    includes.update(pincludes)
                    excludes.update(pexcludes)
                    for subprofile in subprofs:
                        profiles.append(subprofile)

                profiles = visited

            if includes:
                includes.add('.hg*')
            return includes, excludes, profiles

        def getrawprofile(self, profile, changeid):
            try:
                simplecache = extensions.find('simplecache')
                node = self[changeid].hex()
                def func():
                    return self.filectx(profile, changeid=changeid).data()
                key = 'sparseprofile:%s:%s' % (profile.replace('/', '__'), node)
                return simplecache.memoize(func, key,
                        simplecache.stringserializer, self.ui)
            except KeyError:
                return self.filectx(profile, changeid=changeid).data()

        def sparsematch(self, *revs, **kwargs):
            """Returns the sparse match function for the given revs.

            If multiple revs are specified, the match function is the union
            of all the revs.

            `includetemp` is used to indicate if the temporarily included file
            should be part of the matcher.
            """
            if not revs or revs == (None,):
                revs = [self.changelog.rev(node) for node in
                    self.dirstate.parents() if node != nullid]

            try:
                sparsepath = self.opener.join('sparse')
                mtime = os.stat(sparsepath).st_mtime
            except OSError:
                mtime = 0

            tempmtime = 0
            try:
                if kwargs.get('includetemp', True):
                    tempsparsepath = self.opener.join('tempsparse')
                    tempmtime = os.stat(tempsparsepath).st_mtime
            except OSError:
                pass

            key = '%s %s %s' % (str(mtime), str(tempmtime),
                ' '.join([str(r) for r in revs]))
            result = self.sparsecache.get(key, None)
            if result:
                return result

            matchers = []
            for rev in revs:
                try:
                    includes, excludes, profiles = self.getsparsepatterns(rev)

                    if includes or excludes:
                        # Explicitly include subdirectories of includes so
                        # status will walk them down to the actual include.
                        subdirs = set()
                        for include in includes:
                            dirname = os.path.dirname(include)
                            while dirname:
                                subdirs.add(dirname)
                                dirname = os.path.dirname(dirname)

                        matcher = matchmod.match(self.root, '', [],
                            include=includes, exclude=excludes,
                            default='relpath')
                        if subdirs:
                            matcher = forceincludematcher(matcher, subdirs)
                        matchers.append(matcher)
                except IOError:
                    pass

            result = None
            if not matchers:
                result = matchmod.always(self.root, '')
            elif len(matchers) == 1:
                result = matchers[0]
            else:
                result = unionmatcher(matchers)

            if kwargs.get('includetemp', True):
                tempincludes = self.gettemporaryincludes()
                result = forceincludematcher(result, tempincludes)

            self.sparsecache[key] = result

            return result

        def getactiveprofiles(self):
            revs = [self.changelog.rev(node) for node in
                    self.dirstate.parents() if node != nullid]

            activeprofiles = set()
            for rev in revs:
                _, _, profiles = self.getsparsepatterns(rev)
                activeprofiles.update(profiles)

            return activeprofiles

        def writesparseconfig(self, include, exclude, profiles):
            raw = '%s[include]\n%s\n[exclude]\n%s\n' % (
                ''.join(['%%include %s\n' % p for p in sorted(profiles)]),
                '\n'.join(sorted(include)),
                '\n'.join(sorted(exclude)))
            self.opener.write("sparse", raw)

        def addtemporaryincludes(self, files):
            includes = self.gettemporaryincludes()
            for file in files:
                includes.add(file)
            self._writetemporaryincludes(includes)

        def gettemporaryincludes(self):
            existingtemp = set()
            if self.opener.exists('tempsparse'):
                raw = self.opener.read('tempsparse')
                existingtemp.update(raw.split('\n'))
            return existingtemp

        def _writetemporaryincludes(self, includes):
            raw = '\n'.join(sorted(includes))
            self.opener.write('tempsparse', raw)

        def prunetemporaryincludes(self):
            if repo.opener.exists('tempsparse'):
                origstatus = self.status()
                modified, added, removed, deleted, unknown, ignored, clean = origstatus
                if modified or added or removed or deleted:
                    # Still have pending changes. Don't bother trying to prune.
                    return

                sparsematch = self.sparsematch(includetemp=False)
                dirstate = self.dirstate
                actions = []
                dropped = []
                tempincludes = self.gettemporaryincludes()
                for file in tempincludes:
                    if file in dirstate and not sparsematch(file):
                        message = 'dropping temporarily included sparse files'
                        actions.append((file, None, message))
                        dropped.append(file)

                typeactions = collections.defaultdict(list)
                typeactions['r'] = actions
                mergemod.applyupdates(self, typeactions, self[None], self['.'], False)

                # Fix dirstate
                for file in dropped:
                    dirstate.drop(file)

                self.opener.unlink('tempsparse')
                ui.status("cleaned up %d temporarily added file(s) from the sparse checkout\n" %
                    len(tempincludes))


    if 'dirstate' in repo._filecache:
        repo.dirstate.repo = repo
    repo.sparsecache = {}
    repo.__class__ = SparseRepo

@command('^sparse', [
    ('I', 'include', False, _('include files in the sparse checkout')),
    ('X', 'exclude', False, _('exclude files in the sparse checkout')),
    ('d', 'delete', False, _('delete an include/exclude rule')),
    ('f', 'force', False, _('allow changing rules even with pending changes')),
    ('', 'enable-profile', False, _('enables the specified profile')),
    ('', 'disable-profile', False, _('disables the specified profile')),
    ('', 'refresh', False, _('updates the working after sparseness changes')),
    ('', 'reset', False, _('makes the repo full again')),
    ],
    _('[--OPTION] PATTERN...'))
def sparse(ui, repo, *pats, **opts):
    """make the current checkout sparse, or edit the existing checkout

    The sparse command is used to make the current checkout sparse.
    This means files that don't meet the sparse condition will not be
    written to disk, or show up in any working copy operations. It does
    not affect files in history in any way.

    Passing no arguments prints the currently applied sparse rules.

    --include and --exclude are used to add and remove files from the sparse
    checkout. The effects of adding an include or exclude rule are applied
    immediately. If applying the new rule would cause a file with pending
    changes to be added or removed, the command will fail. Pass --force to
    force a rule change even with pending changes (the changes on disk will
    be preserved).

    --delete removes an existing include/exclude rule. The effects are
    immediate.

    --refresh refreshes the files on disk based on the sparse rules. This is
    only necessary if .hg/sparse was changed by hand.

    --enable-profile and --disable-profile accept a path to a .hgsparse file.
    This allows defining sparse checkouts and tracking them inside the
    repository. This is useful for defining commonly used sparse checkouts for
    many people to use. As the profile definition changes over time, the sparse
    checkout will automatically be updated appropriately, depending on which
    commit is checked out. Changes to .hgsparse are not applied until they
    have been committed.

    Returns 0 if editting the sparse checkout succeeds.
    """
    include = opts.get('include')
    exclude = opts.get('exclude')
    force = opts.get('force')
    enableprofile = opts.get('enable_profile')
    disableprofile = opts.get('disable_profile')
    delete = opts.get('delete')
    refresh = opts.get('refresh')
    reset = opts.get('reset')
    count = sum([include, exclude, enableprofile, disableprofile, delete,
                refresh, reset])
    if count > 1:
        raise util.Abort(_("too many flags specified"))

    if count == 0:
        if repo.opener.exists('sparse'):
            ui.status(repo.opener.read("sparse") + "\n")
            temporaryincludes = repo.gettemporaryincludes()
            if temporaryincludes:
                ui.status("Temporarily Included Files (for merge/rebase):\n")
                ui.status("\n".join(temporaryincludes) + "\n")
        else:
            ui.status(_('repo is not sparse\n'))
        return

    if include or exclude or delete or reset or enableprofile or disableprofile:
        _config(ui, repo, pats, include=include, exclude=exclude, reset=reset,
                delete=delete, enableprofile=enableprofile,
                disableprofile=disableprofile, force=force)

    if refresh:
        try:
            wlock = repo.wlock()
            _refresh(ui, repo, repo.status(), repo.sparsematch(), force)
        finally:
            wlock.release()

def _config(ui, repo, pats, include=False, exclude=False, reset=False,
            delete=False, enableprofile=False, disableprofile=False,
            force=False):
    """
    Perform a sparse config update. Only one of the kwargs may be specified.
    """
    wlock = repo.wlock()
    try:
        try:
            oldsparsematch = repo.sparsematch()

            if repo.opener.exists('sparse'):
                raw = repo.opener.read('sparse')
                oldinclude, oldexclude, oldprofiles = repo.readsparseconfig(raw)
            else:
                oldinclude = set()
                oldexclude = set()
                oldprofiles = set()

            if reset:
                newinclude = set()
                newexclude = set()
                newprofiles = set()
            else:
                newinclude = set(oldinclude)
                newexclude = set(oldexclude)
                newprofiles = set(oldprofiles)

            oldstatus = repo.status()

            if include:
                newinclude.update(pats)
            elif exclude:
                newexclude.update(pats)
            elif enableprofile:
                newprofiles.update(pats)
            elif disableprofile:
                newprofiles.difference_update(pats)
            elif delete:
                newinclude.difference_update(pats)
                newexclude.difference_update(pats)

            repo.writesparseconfig(newinclude, newexclude, newprofiles)
            _refresh(ui, repo, oldstatus, oldsparsematch, force)
        except Exception:
            repo.writesparseconfig(oldinclude, oldexclude, oldprofiles)
            raise
    finally:
        wlock.release()

def _refresh(ui, repo, origstatus, origsparsematch, force):
    """Refreshes which files are on disk by comparing the old status and
    sparsematch with the new sparsematch.

    Will raise an exception if a file with pending changes is being excluded
    or included (unless force=True).
    """
    modified, added, removed, deleted, unknown, ignored, clean = origstatus

    # Verify there are no pending changes
    pending = set()
    pending.update(modified)
    pending.update(added)
    pending.update(removed)
    sparsematch = repo.sparsematch()
    abort = False
    for file in pending:
        if not sparsematch(file):
            ui.warn(_("pending changes to '%s'\n") % file)
            abort = not force
    if abort:
        raise util.Abort(_("could not update sparseness due to " +
            "pending changes"))

    # Calculate actions
    dirstate = repo.dirstate
    ctx = repo['.']
    wctx = repo[None]
    added = []
    lookup = []
    dropped = []
    mf = ctx.manifest()
    files = set(mf)

    actions = {}

    for file in files:
        old = origsparsematch(file)
        new = sparsematch(file)
        # Add files that are newly included, or that don't exist in
        # the dirstate yet.
        if (new and not old) or (old and new and not file in dirstate):
            fl = mf.flags(file)
            if repo.wopener.exists(file):
                actions[file] = ('e', (fl,), '')
                lookup.append(file)
            else:
                actions[file] = ('g', (fl,), '')
                added.append(file)
        # Drop files that are newly excluded, or that still exist in
        # the dirstate.
        elif (old and not new) or (not old and not new and file in dirstate):
            dropped.append(file)
            if file not in pending:
                actions[file] = ('r', [], '')

    # Verify there are no pending changes in newly included files
    abort = False
    for file in lookup:
        ui.warn(_("pending changes to '%s'\n") % file)
        abort = not force
    if abort:
        raise util.Abort(_("cannot change sparseness due to " +
            "pending changes (delete the files or use --force " +
            "to bring them back dirty)"))

    # Check for files that were only in the dirstate.
    for file, state in dirstate.iteritems():
        if not file in files:
            old = origsparsematch(file)
            new = sparsematch(file)
            if old and not new:
                dropped.append(file)

    # Apply changes to disk
    typeactions = dict((m, []) for m in 'a f g cd dc r dm dg m e k'.split())
    for f, (m, args, msg) in actions.iteritems():
        if m not in typeactions:
            typeactions[m] = []
        typeactions[m].append((f, args, msg))
    mergemod.applyupdates(repo, typeactions, repo[None], repo['.'], False)

    # Fix dirstate
    for file in added:
        dirstate.normal(file)

    for file in dropped:
        dirstate.drop(file)

    for file in lookup:
        # File exists on disk, and we're bringing it back in an unknown state.
        dirstate.normallookup(file)

class forceincludematcher(object):
    """A matcher that returns true for any of the forced includes before testing
    against the actual matcher."""
    def __init__(self, matcher, includes):
        self._matcher = matcher
        self._includes = includes

    def __call__(self, value):
        return value in self._includes or self._matcher(value)

    def always(self):
        return False

    def files(self):
        return []

    def isexact(self):
        return False

    def anypats(self):
        return True

    def prefix(self):
        return False

    def hash(self):
        sha1 = util.sha1()
        sha1.update(_hashmatcher(self._matcher))
        for include in sorted(self._includes):
            sha1.update(include + '\0')
        return sha1.hexdigest()

class unionmatcher(object):
    """A matcher that is the union of several matchers."""
    def __init__(self, matchers):
        self._matchers = matchers

    def __call__(self, value):
        for match in self._matchers:
            if match(value):
                return True
        return False

    def always(self):
        return False

    def files(self):
        return []

    def isexact(self):
        return False

    def anypats(self):
        return True

    def prefix(self):
        return False

    def hash(self):
        sha1 = util.sha1()
        for m in self._matchers:
            sha1.update(_hashmatcher(m))
        return sha1.hexdigest()

class negatematcher(object):
    def __init__(self, matcher):
        self._matcher = matcher

    def __call__(self, value):
        return not self._matcher(value)

    def always(self):
        return False

    def files(self):
        return []

    def isexact(self):
        return False

    def anypats(self):
        return True

    def hash(self):
        sha1 = util.sha1()
        sha1.update('negate')
        sha1.update(_hashmatcher(self._matcher))
        return sha1.hexdigest()

def _hashmatcher(matcher):
    if util.safehasattr(matcher, 'hash'):
        return matcher.hash()

    sha1 = util.sha1()
    if util.safehasattr(matcher, 'includepat'):
        sha1.update(matcher.includepat)
    sha1.update('\0\0')
    if util.safehasattr(matcher, 'excludepat'):
        sha1.update(matcher.excludepat)
    sha1.update('\0\0')
    if util.safehasattr(matcher, 'patternspat'):
        sha1.update(matcher.patternspat)
    sha1.update('\0\0')
    if util.safehasattr(matcher, '_files'):
        for f in matcher._files:
            sha1.update(f + '\0')
    sha1.update('\0')
    return sha1.hexdigest()

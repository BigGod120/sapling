/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

#![deny(warnings)]

use anyhow::{Error, Result};
use blobrepo::BlobRepo;
use blobstore::Loadable;
use bookmarks::BookmarkName;
use cloned::cloned;
use context::CoreContext;
use futures::{FutureExt, TryFutureExt};
use futures_ext::{spawn_future, BoxFuture, FutureExt as OldFutureExt};
use futures_old::{Future, Stream};
use hooks::{hook_loader::load_hooks, HookManager, HookOutcome};
use hooks_content_stores::blobrepo_text_only_fetcher;
use manifold::{ManifoldHttpClient, PayloadRange};
use mercurial_types::HgChangesetId;
use metaconfig_types::RepoConfig;
use mononoke_types::ChangesetId;
use revset::AncestorsNodeStream;
use scuba_ext::ScubaSampleBuilder;
use slog::{debug, info};
use std::collections::HashSet;
use std::sync::Arc;
use thiserror::Error;

pub struct Tailer {
    ctx: CoreContext,
    repo: BlobRepo,
    hook_manager: Arc<HookManager>,
    bookmark: BookmarkName,
    last_rev_key: String,
    manifold_client: ManifoldHttpClient,
    excludes: HashSet<ChangesetId>,
}

impl Tailer {
    pub fn new(
        ctx: CoreContext,
        repo: BlobRepo,
        config: RepoConfig,
        bookmark: BookmarkName,
        manifold_client: ManifoldHttpClient,
        excludes: HashSet<ChangesetId>,
        disabled_hooks: &HashSet<String>,
    ) -> Result<Tailer> {
        let content_fetcher = blobrepo_text_only_fetcher(repo.clone(), config.hook_max_file_size);

        let mut hook_manager = HookManager::new(
            ctx.fb,
            content_fetcher,
            Default::default(),
            ScubaSampleBuilder::with_discard(),
        );

        load_hooks(ctx.fb, &mut hook_manager, config, disabled_hooks)?;

        let repo_id = repo.get_repoid().id();
        let last_rev_key = format!("{}{}", "__mononoke_hook_tailer_last_rev.", repo_id).to_string();

        Ok(Tailer {
            ctx,
            repo,
            hook_manager: Arc::new(hook_manager),
            bookmark,
            last_rev_key,
            manifold_client,
            excludes,
        })
    }

    pub fn get_last_rev_key(&self) -> String {
        self.last_rev_key.clone()
    }

    pub fn run_single_changeset(
        &self,
        changeset: ChangesetId,
    ) -> BoxFuture<Vec<HookOutcome>, Error> {
        cloned!(self.ctx, self.repo, self.hook_manager, self.bookmark,);
        run_hooks_for_changeset(ctx, repo, hook_manager, bookmark, changeset)
            .map(|(_, result)| result)
            .boxify()
    }

    pub fn run_with_limit(&self, limit: u64) -> BoxFuture<Vec<HookOutcome>, Error> {
        let ctx = self.ctx.clone();
        let bm = self.bookmark.clone();
        let hm = self.hook_manager.clone();
        let excludes = self.excludes.clone();

        let bm_rev = self.repo.get_bonsai_bookmark(ctx.clone(), &bm).and_then({
            cloned!(bm);
            |opt| opt.ok_or(ErrorKind::NoSuchBookmark(bm).into())
        });

        cloned!(self.ctx, self.repo);
        bm_rev
            .and_then(move |bm_rev| {
                AncestorsNodeStream::new(ctx.clone(), &repo.get_changeset_fetcher(), bm_rev)
                    .take(limit)
                    .filter(move |cs| !excludes.contains(cs))
                    .map({
                        move |cs| {
                            cloned!(ctx, bm, hm, repo);
                            run_hooks_for_changeset(ctx, repo, hm, bm, cs)
                        }
                    })
                    .map(spawn_future)
                    .buffered(100)
                    .map(|(_, res)| res)
                    .concat2()
            })
            .boxify()
    }

    pub fn run(&self) -> BoxFuture<Vec<HookOutcome>, Error> {
        info!(
            self.ctx.logger(),
            "Running tailer on bookmark {}",
            self.bookmark.clone()
        );

        self.repo
            .get_bonsai_bookmark(self.ctx.clone(), &self.bookmark.clone())
            .and_then({
                cloned!(self.bookmark);
                |opt| opt.ok_or(ErrorKind::NoSuchBookmark(bookmark).into())
            })
            .and_then({
                cloned!(self.last_rev_key, self.manifold_client);
                move |current_bm_cs| {
                    manifold_client
                        .read(last_rev_key, PayloadRange::Full)
                        .map(move |opt| (current_bm_cs, opt))
                }
            })
            .and_then(|(current_bm_cs, opt)| match opt {
                Some(last_rev_bytes) => Ok((current_bm_cs, last_rev_bytes)),
                None => Err(ErrorKind::NoLastRevision.into()),
            })
            .and_then(|(current_bm_cs, last_rev_bytes)| {
                let node_hash = ChangesetId::from_bytes(&*last_rev_bytes.payload.payload)?;
                Ok((current_bm_cs, node_hash))
            })
            .and_then({
                cloned!(
                    self.bookmark,
                    self.excludes,
                    self.hook_manager,
                    self.repo,
                    self.ctx
                );
                move |(current_bm_cs, last_rev)| {
                    let end_rev = current_bm_cs;
                    info!(
                        ctx.logger(),
                        "Bookmark is currently at {}, last processed revision is {}",
                        end_rev,
                        last_rev
                    );
                    if last_rev == end_rev {
                        info!(ctx.logger(), "Nothing to do");
                    }
                    run_in_range0(
                        ctx,
                        repo,
                        hook_manager,
                        last_rev,
                        end_rev,
                        bookmark,
                        excludes,
                    )
                    .map(move |res| (end_rev, res))
                }
            })
            .and_then({
                cloned!(self.last_rev_key, self.ctx, self.manifold_client);
                move |(end_rev, res)| {
                    info!(
                        ctx.logger(),
                        "Setting last processed revision to {:?}", end_rev
                    );
                    let bytes = end_rev.as_ref().into();
                    manifold_client.write(last_rev_key, bytes).map(|()| res)
                }
            })
            .boxify()
    }
}

fn run_hooks_for_changeset(
    ctx: CoreContext,
    repo: BlobRepo,
    hm: Arc<HookManager>,
    bm: BookmarkName,
    cs_id: ChangesetId,
) -> impl Future<Item = (ChangesetId, Vec<HookOutcome>), Error = Error> {
    cs_id
        .load(ctx.clone(), repo.blobstore())
        .from_err()
        .and_then(move |cs| {
            let ctx = ctx.clone();
            let hm = hm.clone();
            let bm = bm.clone();
            async move {
                debug!(ctx.logger(), "Running hooks for changeset {:?}", cs);
                let hook_results = hm
                    .run_hooks_for_bookmark(&ctx, vec![cs].iter(), &bm, None)
                    .await?;
                Ok((cs_id, hook_results))
            }
            .boxed()
            .compat()
        })
}

fn run_in_range0(
    ctx: CoreContext,
    repo: BlobRepo,
    hm: Arc<HookManager>,
    last_rev: ChangesetId,
    end_rev: ChangesetId,
    bm: BookmarkName,
    excludes: HashSet<ChangesetId>,
) -> BoxFuture<Vec<HookOutcome>, Error> {
    debug!(ctx.logger(), "Running in range {} to {}", last_rev, end_rev);
    AncestorsNodeStream::new(ctx.clone(), &repo.get_changeset_fetcher(), end_rev)
            .take(1000) // Limit number so we don't process too many
            .filter(move |cs| !excludes.contains(cs))
            .map({
                move |cs| {
                    cloned!(ctx, bm, hm, repo);
                    run_hooks_for_changeset(ctx, repo, hm, bm, cs)
                }
            })
            .map(spawn_future)
            .buffered(100)
            .take_while(move |(cs, _)| {
                Ok(*cs != last_rev)
            })
            .map(|(_, res)| res)
            .concat2()
            .boxify()
}

#[derive(Debug, Error)]
pub enum ErrorKind {
    #[error("No such bookmark '{0}'")]
    NoSuchBookmark(BookmarkName),
    #[error("Cannot find last revision in blobstore")]
    NoLastRevision,
    #[error("Cannot find bonsai for {0}")]
    BonsaiNotFound(HgChangesetId),
}

/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

//! Base types used throughout Mononoke.

#![deny(warnings)]

pub mod blame;
pub mod blame_v2;
pub mod blob;
pub mod bonsai_changeset;
pub mod content_chunk;
pub mod content_metadata;
pub mod datetime;
pub mod deleted_files_manifest;
pub mod errors;
pub mod fastlog_batch;
pub mod file_change;
pub mod file_contents;
pub mod fsnode;
pub mod generation;
pub mod globalrev;
pub mod hash;
pub mod path;
pub mod rawbundle2;
pub mod redaction_key_list;
pub mod repo;
pub mod skeleton_manifest;
pub mod sql_types;
pub mod svnrev;
pub mod typed_hash;
pub mod unode;

pub use blame::{Blame, BlameId, BlameRange};
pub use blob::{Blob, BlobstoreValue, ChangesetBlob, ContentBlob, RawBundle2Blob};
pub use blobstore::BlobstoreBytes;
pub use bonsai_changeset::{BonsaiChangeset, BonsaiChangesetMut};
pub use content_chunk::ContentChunk;
pub use content_metadata::{ContentAlias, ContentMetadata};
pub use datetime::{DateTime, Timestamp};
pub use file_change::{BasicFileChange, FileChange, FileType, TrackedFileChange};
pub use file_contents::{ChunkedFileContents, ContentChunkPointer, FileContents};
pub use generation::{Generation, FIRST_GENERATION};
pub use globalrev::Globalrev;
pub use path::{check_case_conflicts, MPath, MPathElement, MPathHash, PrefixTrie, RepoPath};
pub use rawbundle2::RawBundle2;
pub use redaction_key_list::RedactionKeyList;
pub use repo::{RepositoryId, REPO_PREFIX_REGEX};
pub use svnrev::Svnrev;
pub use typed_hash::{
    ChangesetId, ChangesetIdPrefix, ChangesetIdsResolvedFromPrefix, ContentChunkId, ContentId,
    ContentMetadataId, DeletedManifestId, FastlogBatchId, FileUnodeId, FsnodeId, ManifestUnodeId,
    MononokeId, RawBundle2Id, SkeletonManifestId,
};

mod macros;

pub mod thrift {
    pub use mononoke_types_thrift::*;
}

pub mod private {
    pub use anyhow;
    pub use ascii::{AsciiStr, AsciiString};
    pub use quickcheck::{empty_shrinker, Arbitrary, Gen};
    pub use serde::{
        de::Deserialize, de::Deserializer, de::Error as DeError, Serialize, Serializer,
    };

    pub use crate::errors::ErrorKind;
    pub use crate::hash::Blake2;
    pub use crate::thrift;
    pub use crate::typed_hash::Blake2HexVisitor;
}

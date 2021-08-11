/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

#![deny(warnings)]

use anyhow::Result;
use async_trait::async_trait;
use inlinable_string::InlinableString;

use context::CoreContext;

use blobstore::{
    Blobstore, BlobstoreEnumerationData, BlobstoreGetData, BlobstoreIsPresent, BlobstoreKeyParam,
    BlobstoreKeyRange, BlobstoreKeySource, BlobstorePutOps, BlobstoreWithLink, OverwriteStatus,
    PutBehaviour,
};
use mononoke_types::BlobstoreBytes;

/// A layer over an existing blobstore that prepends a fixed string to each get and put.
#[derive(Clone, Debug)]
pub struct PrefixBlobstore<T> {
    // Try to inline the prefix to ensure copies remain cheap. Most prefixes are short anyway.
    prefix: InlinableString,
    blobstore: T,
}

impl<T: std::fmt::Display> std::fmt::Display for PrefixBlobstore<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "PrefixBlobstore<{}>", &self.blobstore)
    }
}

impl<T> PrefixBlobstore<T> {
    pub fn into_inner(self) -> T {
        self.blobstore
    }

    pub fn as_inner(&self) -> &T {
        &self.blobstore
    }

    pub fn prefix(&self) -> String {
        self.prefix.to_string()
    }
}

impl<T> PrefixBlobstore<T> {
    pub fn new<S: Into<InlinableString>>(blobstore: T, prefix: S) -> Self {
        let prefix = prefix.into();
        Self { prefix, blobstore }
    }

    #[inline]
    pub fn prepend(&self, key: impl AsRef<str>) -> String {
        [&self.prefix, key.as_ref()].concat()
    }

    #[inline]
    pub fn unprepend(&self, key: &str) -> String {
        key[self.prefix.len()..].to_string()
    }
}

#[async_trait]
impl<T: Blobstore> Blobstore for PrefixBlobstore<T> {
    #[inline]
    async fn get<'a>(
        &'a self,
        ctx: &'a CoreContext,
        key: &'a str,
    ) -> Result<Option<BlobstoreGetData>> {
        self.blobstore.get(ctx, &self.prepend(key)).await
    }

    #[inline]
    async fn put<'a>(
        &'a self,
        ctx: &'a CoreContext,
        key: String,
        value: BlobstoreBytes,
    ) -> Result<()> {
        self.blobstore.put(ctx, self.prepend(key), value).await
    }

    #[inline]
    async fn is_present<'a>(
        &'a self,
        ctx: &'a CoreContext,
        key: &'a str,
    ) -> Result<BlobstoreIsPresent> {
        self.blobstore.is_present(ctx, &self.prepend(key)).await
    }
}

#[async_trait]
impl<T: BlobstorePutOps> BlobstorePutOps for PrefixBlobstore<T> {
    async fn put_explicit<'a>(
        &'a self,
        ctx: &'a CoreContext,
        key: String,
        value: BlobstoreBytes,
        put_behaviour: PutBehaviour,
    ) -> Result<OverwriteStatus> {
        self.blobstore
            .put_explicit(ctx, self.prepend(key), value, put_behaviour)
            .await
    }

    async fn put_with_status<'a>(
        &'a self,
        ctx: &'a CoreContext,
        key: String,
        value: BlobstoreBytes,
    ) -> Result<OverwriteStatus> {
        self.blobstore
            .put_with_status(ctx, self.prepend(key), value)
            .await
    }
}

#[async_trait]
impl<T: BlobstoreWithLink> BlobstoreWithLink for PrefixBlobstore<T> {
    async fn link<'a>(
        &'a self,
        ctx: &'a CoreContext,
        existing_key: &'a str,
        link_key: String,
    ) -> Result<()> {
        self.blobstore
            .link(ctx, &self.prepend(existing_key), self.prepend(link_key))
            .await
    }

    async fn unlink<'a>(&'a self, ctx: &'a CoreContext, key: &'a str) -> Result<()> {
        self.blobstore.unlink(ctx, &self.prepend(key)).await
    }
}

#[async_trait]
impl<T: BlobstoreKeySource> BlobstoreKeySource for PrefixBlobstore<T> {
    async fn enumerate<'a>(
        &'a self,
        ctx: &'a CoreContext,
        range: &'a BlobstoreKeyParam,
    ) -> Result<BlobstoreEnumerationData> {
        let new_param = match range {
            BlobstoreKeyParam::Start(range) => BlobstoreKeyParam::Start(BlobstoreKeyRange {
                begin_key: if range.begin_key.is_empty() {
                    String::new()
                } else {
                    self.prepend(&range.begin_key)
                },
                end_key: if range.end_key.is_empty() {
                    String::new()
                } else {
                    self.prepend(&range.end_key)
                },
            }),
            // No need to prepend Continuation as we don't unprepend it
            p => p.clone(),
        };
        let mut res = self.blobstore.enumerate(ctx, &new_param).await?;
        res.keys = res.keys.into_iter().map(|k| self.unprepend(&k)).collect();
        Ok(res)
    }
}

#[cfg(test)]
mod test {
    use super::*;

    use borrowed::borrowed;
    use bytes::Bytes;
    use fbinit::FacebookInit;
    use maplit::hashset;

    use memblob::Memblob;

    #[fbinit::test]
    async fn test_prefix(fb: FacebookInit) {
        let ctx = CoreContext::test_mock(fb);
        borrowed!(ctx);
        let base = Memblob::default();
        let prefixed = PrefixBlobstore::new(base.clone(), "prefix123-");
        let unprefixed_key = "foobar".to_string();
        let prefixed_key = "prefix123-foobar".to_string();

        prefixed
            .put(
                ctx,
                unprefixed_key.clone(),
                BlobstoreBytes::from_bytes("test foobar"),
            )
            .await
            .expect("put should succeed");

        // Test that both the prefixed and the unprefixed stores can access the key.
        assert_eq!(
            prefixed
                .get(ctx, &unprefixed_key)
                .await
                .expect("get should succeed")
                .expect("value should be present")
                .into_raw_bytes(),
            Bytes::from("test foobar"),
        );
        assert_eq!(
            base.get(ctx, &prefixed_key)
                .await
                .expect("get should succeed")
                .expect("value should be present")
                .into_raw_bytes(),
            Bytes::from("test foobar"),
        );

        // Test that is_present works for both the prefixed and unprefixed stores.
        assert!(
            prefixed
                .is_present(ctx, &unprefixed_key)
                .await
                .expect("is_present should succeed")
                .assume_not_found_if_unsure()
        );
        assert!(
            base.is_present(ctx, &prefixed_key)
                .await
                .expect("is_present should succeed")
                .assume_not_found_if_unsure()
        );

        let enumerated = prefixed
            .enumerate(ctx, &BlobstoreKeyParam::from(..))
            .await
            .unwrap();

        assert_eq!(enumerated.keys, hashset! { unprefixed_key.clone() });

        assert!(
            prefixed
                .enumerate(ctx, &BlobstoreKeyParam::from("foobar1".to_string()..))
                .await
                .unwrap()
                .keys
                .is_empty()
        );

        assert!(
            !prefixed
                .enumerate(ctx, &BlobstoreKeyParam::from("fooba".to_string()..))
                .await
                .unwrap()
                .keys
                .is_empty()
        );
    }
}

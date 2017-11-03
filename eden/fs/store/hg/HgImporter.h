/*
 *  Copyright (c) 2016-present, Facebook, Inc.
 *  All rights reserved.
 *
 *  This source code is licensed under the BSD-style license found in the
 *  LICENSE file in the root directory of this source tree. An additional grant
 *  of patent rights can be found in the PATENTS file in the same directory.
 *
 */
#pragma once

#include <folly/Range.h>
#include <folly/Subprocess.h>

#include "eden/fs/store/LocalStore.h"
#include "eden/fs/utils/PathFuncs.h"

namespace folly {
class IOBuf;
namespace io {
class Cursor;
}
} // namespace folly

/* forward declare support classes from mercurial */
class DatapackStore;
class UnionDatapackStore;

namespace facebook {
namespace eden {

class Hash;
class HgManifestImporter;
class StoreResult;
class Tree;

/**
 * HgImporter provides an API for extracting data out of a mercurial
 * repository.
 *
 * Mercurial itself is in python, so some of the import logic runs as python
 * code.  HgImporter hides all of the interaction with the underlying python
 * code.
 *
 * HgImporter is not thread safe.  The external caller must provide their own
 * locking around each HgImporter object.  However, to achieve parallelism
 * multiple HgImporter objects can be created for the same repository and used
 * simultaneously.
 */
class HgImporter {
 public:
  /**
   * Create a new HgImporter object that will import data from the specified
   * repository into the given LocalStore.
   *
   * The caller is responsible for ensuring that the LocalStore object remains
   * valid for the lifetime of the HgImporter object.
   */
  HgImporter(AbsolutePathPiece repoPath, LocalStore* store);
  virtual ~HgImporter();

  /**
   * Import the manifest for the specified revision.
   *
   * Returns a Hash identifying the root Tree for the imported revision.
   */
  Hash importManifest(folly::StringPiece revName);

  /**
   * Import the manifest for the specified revision using mercurial
   * treemanifest data.
   *
   * Most callers should use the importManifest() function above, which
   * automatically chooses the best mechanism to use for importing tree data.
   * This method is exposed publicly primarily for testing purposes.
   */
  Hash importTreeManifest(folly::StringPiece revName);

  /**
   * Import the manifest for the specified revision using mercurial
   * flat manifest data.
   *
   * Most callers should use the importManifest() function above, which
   * automatically chooses the best mechanism to use for importing tree data.
   * This method is exposed publicly primarily for testing purposes.
   */
  Hash importFlatManifest(folly::StringPiece revName);

  /**
   * Import flat manifest data from the specified input File, and put the data
   * into the specified LocalStore object.
   *
   * This API is primarily intended to allow benchmarking the flat manifest
   * import process by importing data from a pre-generated file.  Outside of
   * benchmarking the importFlatManifest() function above should generally be
   * used instead.
   */
  static Hash importFlatManifest(int manifestDataFd, LocalStore* store);

  /**
   * Import the tree with the specified tree manifest hash.
   *
   * @param id The Tree ID.  Note that this is eden's Tree ID, and does not
   *   correspond to the mercurial manifest node ID for this path.
   *
   * Returns the Tree, or throws on error.
   * Requires that tree manifest data be available.
   */
  std::unique_ptr<Tree> importTree(const Hash& id);

  /**
   * Import file information
   *
   * Takes a hash identifying the requested blob.  (For instance, blob hashes
   * can be found in the TreeEntry objects generated by importManifest().)
   *
   * Returns an IOBuf containing the file contents.
   */
  folly::IOBuf importFileContents(Hash blobHash);

  /**
   * Resolve the manifest node for the specified revision.
   *
   * This is used to locate the mercurial tree manifest data for
   * the root tree of a given commit.
   *
   * Returns a Hash identifying the manifest node for the revision.
   */
  Hash resolveManifestNode(folly::StringPiece revName);

 private:
  /**
   * Chunk header flags.
   *
   * These are flag values, designed to be bitwise ORed with each other.
   */
  enum : uint32_t {
    FLAG_ERROR = 0x01,
    FLAG_MORE_CHUNKS = 0x02,
  };
  /**
   * hg_import_helper protocol version number.
   *
   * Bump this whenever you add new commands or change the command parameters
   * or response data.  This helps us identify if edenfs somehow ends up
   * using an incompatible version of the hg_import_helper script.
   *
   * This must be kept in sync with the PROTOCOL_VERSION field in
   * hg_import_helper.py
   */
  enum : uint32_t {
    PROTOCOL_VERSION = 1,
  };
  /**
   * Flags for the CMD_STARTED response
   */
  enum StartFlag : uint32_t {
    TREEMANIFEST_SUPPORTED = 0x01,
  };
  /**
   * Command type values.
   *
   * See hg_import_helper.py for a more complete description of the
   * request/response formats.
   */
  enum : uint32_t {
    CMD_STARTED = 0,
    CMD_RESPONSE = 1,
    CMD_MANIFEST = 2,
    CMD_CAT_FILE = 3,
    CMD_MANIFEST_NODE_FOR_COMMIT = 4,
    CMD_FETCH_TREE = 5,
  };
  struct ChunkHeader {
    uint32_t requestID;
    uint32_t command;
    uint32_t flags;
    uint32_t dataLength;
  };

  /**
   * Options for this HgImporter.
   *
   * This is parsed from the initial CMD_STARTED response from the
   * hg_import_helper process, and contains details about the configuration
   * for this mercurial repository.
   */
  struct Options {
    /**
     * The paths to the treemanifest pack directories.
     * If this vector is empty treemanifest import should not be used.
     */
    std::vector<std::string> treeManifestPackPaths;
  };

  // Forbidden copy constructor and assignment operator
  HgImporter(const HgImporter&) = delete;
  HgImporter& operator=(const HgImporter&) = delete;

  /**
   * Read a single manifest entry from a manifest response chunk,
   * and give it to the HgManifestImporter for processing.
   *
   * The cursor argument points to the start of the manifest entry in the
   * response chunk received from the helper process.  readManifestEntry() is
   * responsible for updating the cursor to point to the next manifest entry.
   */
  static void readManifestEntry(
      HgManifestImporter& importer,
      folly::io::Cursor& cursor,
      LocalStore::WriteBatch& writeBatch);
  /**
   * Read a response chunk header from the helper process
   *
   * If the header indicates an error, this will read the full error message
   * and throw a std::runtime_error.
   */
  ChunkHeader readChunkHeader() {
    return readChunkHeader(helperOut_);
  }
  static ChunkHeader readChunkHeader(int fd);

  /**
   * Wait for the helper process to send a CMD_STARTED response to indicate
   * that it has started successfully.  Process the response and finish
   * setting up member variables based on the data included in the response.
   */
  Options waitForHelperStart();

  /**
   * Initialize the unionStore_ needed for treemanifest import support.
   *
   * This leaves unionStore_ null if treemanifest import is not supported in
   * this repository.
   */
  void initializeTreeManifestImport(const Options& options);

  /**
   * Send a request to the helper process, asking it to send us the manifest
   * for the specified revision.
   */
  void sendManifestRequest(folly::StringPiece revName);
  /**
   * Send a request to the helper process, asking it to send us the contents
   * of the given file at the specified file revision.
   */
  void sendFileRequest(RelativePathPiece path, Hash fileRevHash);
  /**
   * Send a request to the helper process, asking it to send us the
   * manifest node (NOT the full manifest!) for the specified revision.
   */
  void sendManifestNodeRequest(folly::StringPiece revName);
  /**
   * Send a request to the helper process asking it to prefetch data for trees
   * under the specified path, at the specified manifest node for the given
   * path.
   */
  void sendFetchTreeRequest(RelativePathPiece path, Hash pathManifestNode);

  std::unique_ptr<Tree> importTreeImpl(
      const Hash& manifestNode,
      const Hash& edenTreeID,
      RelativePathPiece path,
      LocalStore::WriteBatch& writeBatch);

  folly::Subprocess helper_;
  const AbsolutePath repoPath_;
  LocalStore* const store_{nullptr};
  uint32_t nextRequestID_{0};
  /**
   * The input and output file descriptors to the helper subprocess.
   * We don't own these FDs, and don't need to close them--they will be closed
   * automatically by the Subprocess object.
   *
   * We simply cache them as member variables to avoid having to look them up
   * via helper_.parentFd() each time we need to use them.
   */
  int helperIn_{-1};
  int helperOut_{-1};

  std::vector<std::unique_ptr<DatapackStore>> dataPackStores_;
  std::unique_ptr<UnionDatapackStore> unionStore_;
};
} // namespace eden
} // namespace facebook

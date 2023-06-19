// Copyright (c) Meta Platforms, Inc. and affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include "fairseq2/native/data/filtered_data_source.h"

namespace fairseq2::detail {

std::optional<data>
filtered_data_source::next()
{
    std::optional<data> d{};

    while ((d = inner_->next()))
        if (invoke_predicate_fn(*d))
            break;

    return d;
}

void
filtered_data_source::reset()
{
    inner_->reset();
}

void
filtered_data_source::record_position(tape &t) const
{
    inner_->record_position(t);
}

void
filtered_data_source::reload_position(tape &t)
{
    inner_->reload_position(t);
}

bool
filtered_data_source::invoke_predicate_fn(data &example)
{
    try {
        return fn_(example);
    } catch (const data_pipeline_error &) {
        throw;
    } catch (...) {
        data_pipeline_error::throw_nested(
            "The predicate function has failed.", std::move(example));
    }
}

} // fairseq2::detail

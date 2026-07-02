#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace {

struct Bits {
    std::vector<uint64_t> w;

    Bits() = default;
    explicit Bits(size_t words) : w(words, 0) {}

    bool operator==(const Bits& other) const noexcept { return w == other.w; }
};

struct BitsHash {
    size_t operator()(const Bits& b) const noexcept {
        // SplitMix64-style mixing over fixed-width words.
        uint64_t h = 0x9e3779b97f4a7c15ULL ^ static_cast<uint64_t>(b.w.size());
        for (uint64_t x : b.w) {
            x += 0x9e3779b97f4a7c15ULL;
            x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
            x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
            x ^= (x >> 31);
            h ^= x + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
        }
        return static_cast<size_t>(h);
    }
};

inline void mask_unused_high_bits(Bits& b, size_t bit_count) {
    const size_t extra = bit_count & 63U;
    if (extra == 0 || b.w.empty()) return;
    const uint64_t mask = (1ULL << extra) - 1ULL;
    b.w.back() &= mask;
}

inline bool is_zero(const Bits& b) {
    for (uint64_t x : b.w) {
        if (x != 0) return false;
    }
    return true;
}

inline void set_bit(Bits& b, size_t bit) {
    b.w[bit >> 6U] |= (1ULL << (bit & 63U));
}

inline void clear_bit(Bits& b, size_t bit) {
    b.w[bit >> 6U] &= ~(1ULL << (bit & 63U));
}

inline bool test_bit(const Bits& b, size_t bit) {
    return (b.w[bit >> 6U] >> (bit & 63U)) & 1ULL;
}

inline void or_inplace(Bits& dst, const Bits& src) {
    for (size_t i = 0; i < dst.w.size(); ++i) dst.w[i] |= src.w[i];
}

inline void and_not_inplace(Bits& dst, const Bits& mask) {
    for (size_t i = 0; i < dst.w.size(); ++i) dst.w[i] &= ~mask.w[i];
}

inline uint32_t popcount64(uint64_t x) {
#if defined(__GNUC__) || defined(__clang__)
    return static_cast<uint32_t>(__builtin_popcountll(x));
#else
    uint32_t c = 0;
    while (x) { x &= x - 1; ++c; }
    return c;
#endif
}

inline size_t bit_count(const Bits& b) {
    size_t total = 0;
    for (uint64_t x : b.w) total += popcount64(x);
    return total;
}

inline int ctz64(uint64_t x) {
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_ctzll(x);
#else
    int n = 0;
    while ((x & 1ULL) == 0ULL) { x >>= 1; ++n; }
    return n;
#endif
}

inline size_t lowest_set_bit(const Bits& b) {
    for (size_t word = 0; word < b.w.size(); ++word) {
        uint64_t x = b.w[word];
        if (x) return word * 64U + static_cast<size_t>(ctz64(x));
    }
    return static_cast<size_t>(-1);
}

static size_t py_int_bit_length(PyObject* obj) {
    PyObject* result = PyObject_CallMethod(obj, "bit_length", nullptr);
    if (!result) throw std::runtime_error("failed to call int.bit_length()");
    const Py_ssize_t n = PyLong_AsSsize_t(result);
    Py_DECREF(result);
    if (n < 0 && PyErr_Occurred()) throw std::runtime_error("failed to convert bit_length");
    return static_cast<size_t>(n);
}

static size_t max_bit_length_in_sequence(PyObject* seq_obj) {
    PyObject* seq = PySequence_Fast(seq_obj, "expected a sequence of Python ints");
    if (!seq) throw std::runtime_error("expected a sequence of Python ints");
    const Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    size_t max_bits = 0;
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject* item = PySequence_Fast_GET_ITEM(seq, i);
        max_bits = std::max(max_bits, py_int_bit_length(item));
    }
    Py_DECREF(seq);
    return max_bits;
}

static Bits py_int_to_bits(PyObject* obj, size_t word_count) {
    Bits b(word_count);
    if (word_count == 0) return b;
    if (word_count == 1) {
        unsigned long long v = PyLong_AsUnsignedLongLong(obj);
        if (PyErr_Occurred()) {
            PyErr_Clear();
        } else {
            b.w[0] = static_cast<uint64_t>(v);
            return b;
        }
    }

    PyObject* length = PyLong_FromSize_t(word_count * 8U);
    PyObject* byteorder = PyUnicode_FromString("little");
    if (!length || !byteorder) {
        Py_XDECREF(length);
        Py_XDECREF(byteorder);
        throw std::runtime_error("failed to allocate int.to_bytes args");
    }
    PyObject* method = PyObject_GetAttrString(obj, "to_bytes");
    if (!method) {
        Py_DECREF(length);
        Py_DECREF(byteorder);
        throw std::runtime_error("object has no to_bytes method");
    }
    PyObject* bytes = PyObject_CallFunctionObjArgs(method, length, byteorder, nullptr);
    Py_DECREF(method);
    Py_DECREF(length);
    Py_DECREF(byteorder);
    if (!bytes) throw std::runtime_error("failed to call int.to_bytes()");
    char* data = PyBytes_AsString(bytes);
    const Py_ssize_t size = PyBytes_Size(bytes);
    if (!data || size < 0) {
        Py_DECREF(bytes);
        throw std::runtime_error("failed to read int bytes");
    }
    const size_t usable = std::min(static_cast<size_t>(size), word_count * 8U);
    for (size_t i = 0; i < usable; ++i) {
        b.w[i >> 3U] |= (static_cast<uint64_t>(static_cast<unsigned char>(data[i])) << ((i & 7U) * 8U));
    }
    Py_DECREF(bytes);
    return b;
}

static std::vector<Bits> parse_int_mask_sequence(PyObject* seq_obj, size_t word_count) {
    PyObject* seq = PySequence_Fast(seq_obj, "expected a sequence of Python ints");
    if (!seq) throw std::runtime_error("expected a sequence of Python ints");
    const Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    std::vector<Bits> out;
    out.reserve(static_cast<size_t>(n));
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject* item = PySequence_Fast_GET_ITEM(seq, i);
        if (!PyLong_Check(item)) {
            Py_DECREF(seq);
            throw std::runtime_error("mask sequence contains a non-int item");
        }
        out.emplace_back(py_int_to_bits(item, word_count));
    }
    Py_DECREF(seq);
    return out;
}

static PyObject* bits_to_index_list(const Bits& b, size_t max_bits) {
    const size_t count = bit_count(b);
    PyObject* list = PyList_New(static_cast<Py_ssize_t>(count));
    if (!list) return nullptr;
    Py_ssize_t pos = 0;
    const size_t max_word = std::min(b.w.size(), (max_bits + 63U) / 64U);
    for (size_t word = 0; word < max_word; ++word) {
        uint64_t x = b.w[word];
        while (x) {
            const uint64_t bit = x & (~x + 1ULL);
            const size_t index = word * 64U + static_cast<size_t>(ctz64(x));
            if (index < max_bits) {
                PyObject* item = PyLong_FromSize_t(index);
                if (!item) {
                    Py_DECREF(list);
                    return nullptr;
                }
                PyList_SET_ITEM(list, pos++, item); // steals reference
            }
            x ^= bit;
        }
    }
    if (pos != static_cast<Py_ssize_t>(count)) {
        // This should only happen if high unused bits slipped in. Shrink by copying.
        PyObject* trimmed = PyList_GetSlice(list, 0, pos);
        Py_DECREF(list);
        return trimmed;
    }
    return list;
}

static std::string bits_to_hex(const Bits& b) {
    int high = static_cast<int>(b.w.size()) - 1;
    while (high >= 0 && b.w[static_cast<size_t>(high)] == 0ULL) --high;
    if (high < 0) return "0";
    std::ostringstream oss;
    oss << std::hex << std::nouppercase << b.w[static_cast<size_t>(high)];
    for (int i = high - 1; i >= 0; --i) {
        oss << std::setw(16) << std::setfill('0') << std::hex << std::nouppercase << b.w[static_cast<size_t>(i)];
    }
    return oss.str();
}

class Enumerator {
public:
    size_t unit_count = 0;
    size_t unit_word_count = 0;
    size_t atom_bit_count = 0;
    size_t bond_bit_count = 0;
    std::vector<Bits> atom_masks;
    std::vector<Bits> bond_masks;
    std::vector<Bits> adjacency_masks;
    std::vector<Bits> closure_masks;
    int min_atoms = 3;
    int max_atoms = 0;
    int shard_index = 0;
    int shard_count = 1;
    int root_unit_index = -1;
    int root_bucket_index = 0;
    int root_bucket_count = 1;
    long long limit_states = -1;
    long long limit_fragments = -1;
    std::deque<Bits> queue;
    std::unordered_set<Bits, BitsHash> queued;
    std::unordered_set<Bits, BitsHash> visited;
    long long emitted = 0;
    bool stopped = false;

    void initialize(PyObject* py_atom_masks,
                    PyObject* py_bond_masks,
                    PyObject* py_adjacency_masks,
                    PyObject* py_closure_masks,
                    int min_atoms_,
                    int max_atoms_,
                    int shard_index_,
                    int shard_count_,
                    int root_unit_index_,
                    int root_bucket_index_,
                    int root_bucket_count_,
                    long long limit_states_,
                    long long limit_fragments_) {
        unit_count = static_cast<size_t>(PySequence_Length(py_atom_masks));
        if (unit_count == static_cast<size_t>(-1)) throw std::runtime_error("atom_masks must be a sequence");
        if (PySequence_Length(py_bond_masks) != static_cast<Py_ssize_t>(unit_count) ||
            PySequence_Length(py_adjacency_masks) != static_cast<Py_ssize_t>(unit_count) ||
            PySequence_Length(py_closure_masks) != static_cast<Py_ssize_t>(unit_count)) {
            throw std::runtime_error("all unit mask sequences must have the same length");
        }
        unit_word_count = std::max<size_t>(1, (unit_count + 63U) / 64U);
        atom_bit_count = std::max<size_t>(1, max_bit_length_in_sequence(py_atom_masks));
        bond_bit_count = std::max<size_t>(1, max_bit_length_in_sequence(py_bond_masks));
        const size_t atom_word_count = std::max<size_t>(1, (atom_bit_count + 63U) / 64U);
        const size_t bond_word_count = std::max<size_t>(1, (bond_bit_count + 63U) / 64U);

        atom_masks = parse_int_mask_sequence(py_atom_masks, atom_word_count);
        bond_masks = parse_int_mask_sequence(py_bond_masks, bond_word_count);
        adjacency_masks = parse_int_mask_sequence(py_adjacency_masks, unit_word_count);
        closure_masks = parse_int_mask_sequence(py_closure_masks, unit_word_count);
        for (Bits& b : adjacency_masks) mask_unused_high_bits(b, unit_count);
        for (Bits& b : closure_masks) mask_unused_high_bits(b, unit_count);

        min_atoms = min_atoms_;
        max_atoms = max_atoms_;
        shard_index = shard_index_;
        shard_count = std::max(1, shard_count_);
        root_unit_index = root_unit_index_;
        root_bucket_index = root_bucket_index_;
        root_bucket_count = std::max(1, root_bucket_count_);
        limit_states = limit_states_;
        limit_fragments = limit_fragments_;

        // Reserve modestly to reduce rehashing on large shards without forcing huge upfront memory.
        visited.reserve(8192);
        queued.reserve(8192);

        if (root_unit_index < 0) {
            for (size_t i = 0; i < unit_count; ++i) {
                Bits s(unit_word_count);
                set_bit(s, i);
                enqueue(std::move(s));
            }
        } else {
            if (root_unit_index >= static_cast<int>(unit_count)) {
                stopped = true;
                return;
            }
            Bits s(unit_word_count);
            set_bit(s, static_cast<size_t>(root_unit_index));
            enqueue(std::move(s));
        }
    }

    Bits close_state(const Bits& state) const {
        Bits closed = state;
        for_each_set_bit(state, unit_count, [&](size_t unit_idx) {
            or_inplace(closed, closure_masks[unit_idx]);
        });
        mask_unused_high_bits(closed, unit_count);
        return closed;
    }

    template <typename Fn>
    static void for_each_set_bit(const Bits& b, size_t max_bits, Fn&& fn) {
        const size_t max_word = std::min(b.w.size(), (max_bits + 63U) / 64U);
        for (size_t word = 0; word < max_word; ++word) {
            uint64_t x = b.w[word];
            while (x) {
                const int offset = ctz64(x);
                const size_t bit_index = word * 64U + static_cast<size_t>(offset);
                if (bit_index < max_bits) fn(bit_index);
                x &= (x - 1ULL);
            }
        }
    }

    int second_unit_bucket(const Bits& state, size_t root_unit) const {
        Bits without_root = state;
        clear_bit(without_root, root_unit);
        if (is_zero(without_root)) return 0;
        return static_cast<int>(lowest_set_bit(without_root) % static_cast<size_t>(root_bucket_count));
    }

    bool owner_matches(const Bits& closed) const {
        const size_t first = lowest_set_bit(closed);
        if (first == static_cast<size_t>(-1)) return false;
        if (root_unit_index < 0) {
            return static_cast<int>(first % static_cast<size_t>(shard_count)) == shard_index;
        }
        if (static_cast<int>(first) != root_unit_index) return false;
        const bool singleton = test_bit(closed, static_cast<size_t>(root_unit_index)) && bit_count(closed) == 1U;
        if (!singleton && second_unit_bucket(closed, static_cast<size_t>(root_unit_index)) != root_bucket_index) return false;
        return true;
    }

    void enqueue(Bits&& state) {
        Bits closed = close_state(state);
        if (!owner_matches(closed)) return;
        if (visited.find(closed) != visited.end()) return;
        auto inserted = queued.insert(closed);
        if (inserted.second) queue.push_back(std::move(closed));
    }

    void atom_bond_masks_for_state(const Bits& state, Bits& atom_mask, Bits& bond_mask) const {
        for_each_set_bit(state, unit_count, [&](size_t unit_idx) {
            or_inplace(atom_mask, atom_masks[unit_idx]);
            or_inplace(bond_mask, bond_masks[unit_idx]);
        });
        mask_unused_high_bits(atom_mask, atom_bit_count);
        mask_unused_high_bits(bond_mask, bond_bit_count);
    }

    PyObject* next_batch(size_t max_records) {
        PyObject* state_keys = PyList_New(0);
        PyObject* atom_lists = PyList_New(0);
        PyObject* bond_lists = PyList_New(0);
        PyObject* atom_counts = PyList_New(0);
        PyObject* bond_counts = PyList_New(0);
        if (!state_keys || !atom_lists || !bond_lists || !atom_counts || !bond_counts) {
            Py_XDECREF(state_keys); Py_XDECREF(atom_lists); Py_XDECREF(bond_lists); Py_XDECREF(atom_counts); Py_XDECREF(bond_counts);
            return nullptr;
        }

        size_t produced = 0;
        while (!stopped && produced < max_records && !queue.empty()) {
            Bits state = std::move(queue.front());
            queue.pop_front();
            queued.erase(state);
            if (visited.find(state) != visited.end()) continue;
            visited.insert(state);

            if (limit_states >= 0 && static_cast<long long>(visited.size()) > limit_states) {
                stopped = true;
                break;
            }

            Bits atom_mask((atom_bit_count + 63U) / 64U);
            Bits bond_mask((bond_bit_count + 63U) / 64U);
            atom_bond_masks_for_state(state, atom_mask, bond_mask);
            const size_t atom_n = bit_count(atom_mask);
            if (atom_n <= static_cast<size_t>(max_atoms)) {
                bool should_emit = true;
                if (root_unit_index >= 0 && test_bit(state, static_cast<size_t>(root_unit_index)) && bit_count(state) == 1U && root_bucket_index != 0) {
                    should_emit = false;
                }
                if (should_emit && atom_n >= static_cast<size_t>(min_atoms)) {
                    PyObject* py_state = PyUnicode_FromString(bits_to_hex(state).c_str());
                    PyObject* py_atoms = bits_to_index_list(atom_mask, atom_bit_count);
                    PyObject* py_bonds = bits_to_index_list(bond_mask, bond_bit_count);
                    PyObject* py_atom_count = PyLong_FromSize_t(atom_n);
                    PyObject* py_bond_count = PyLong_FromSize_t(bit_count(bond_mask));
                    if (!py_state || !py_atoms || !py_bonds || !py_atom_count || !py_bond_count) {
                        Py_XDECREF(py_state); Py_XDECREF(py_atoms); Py_XDECREF(py_bonds); Py_XDECREF(py_atom_count); Py_XDECREF(py_bond_count);
                        Py_DECREF(state_keys); Py_DECREF(atom_lists); Py_DECREF(bond_lists); Py_DECREF(atom_counts); Py_DECREF(bond_counts);
                        return nullptr;
                    }
                    if (PyList_Append(state_keys, py_state) < 0 ||
                        PyList_Append(atom_lists, py_atoms) < 0 ||
                        PyList_Append(bond_lists, py_bonds) < 0 ||
                        PyList_Append(atom_counts, py_atom_count) < 0 ||
                        PyList_Append(bond_counts, py_bond_count) < 0) {
                        Py_DECREF(py_state); Py_DECREF(py_atoms); Py_DECREF(py_bonds); Py_DECREF(py_atom_count); Py_DECREF(py_bond_count);
                        Py_DECREF(state_keys); Py_DECREF(atom_lists); Py_DECREF(bond_lists); Py_DECREF(atom_counts); Py_DECREF(bond_counts);
                        return nullptr;
                    }
                    Py_DECREF(py_state); Py_DECREF(py_atoms); Py_DECREF(py_bonds); Py_DECREF(py_atom_count); Py_DECREF(py_bond_count);
                    ++emitted;
                    ++produced;
                    if (limit_fragments >= 0 && emitted >= limit_fragments) {
                        stopped = true;
                        break;
                    }
                    if (produced >= max_records) {
                        // Keep expansion semantics identical: emitted state still expands before yielding next batch.
                    }
                }

                if (!stopped) {
                    Bits frontier(unit_word_count);
                    for_each_set_bit(state, unit_count, [&](size_t unit_idx) {
                        or_inplace(frontier, adjacency_masks[unit_idx]);
                    });
                    and_not_inplace(frontier, state);
                    mask_unused_high_bits(frontier, unit_count);
                    for_each_set_bit(frontier, unit_count, [&](size_t next_unit_idx) {
                        Bits next = state;
                        set_bit(next, next_unit_idx);
                        enqueue(std::move(next));
                    });
                }
            }
            // Python implementation also skips expansion once atom_count > max_atoms.
        }

        PyObject* result = PyDict_New();
        if (!result) {
            Py_DECREF(state_keys); Py_DECREF(atom_lists); Py_DECREF(bond_lists); Py_DECREF(atom_counts); Py_DECREF(bond_counts);
            return nullptr;
        }
        PyDict_SetItemString(result, "state_keys", state_keys);
        PyDict_SetItemString(result, "atom_indices", atom_lists);
        PyDict_SetItemString(result, "bond_indices", bond_lists);
        PyDict_SetItemString(result, "atom_counts", atom_counts);
        PyDict_SetItemString(result, "bond_counts", bond_counts);
        PyObject* py_visited = PyLong_FromSize_t(visited.size());
        PyObject* py_emitted = PyLong_FromLongLong(emitted);
        PyObject* py_done = (stopped || queue.empty()) ? Py_True : Py_False;
        Py_INCREF(py_done);
        PyDict_SetItemString(result, "visited_state_count", py_visited);
        PyDict_SetItemString(result, "emitted_fragment_count", py_emitted);
        PyDict_SetItemString(result, "done", py_done);
        Py_DECREF(py_visited); Py_DECREF(py_emitted); Py_DECREF(py_done);
        Py_DECREF(state_keys); Py_DECREF(atom_lists); Py_DECREF(bond_lists); Py_DECREF(atom_counts); Py_DECREF(bond_counts);
        return result;
    }
};

typedef struct {
    PyObject_HEAD
    Enumerator* impl;
} FastEnumeratorObject;

static PyObject* FastEnumerator_new(PyTypeObject* type, PyObject*, PyObject*) {
    FastEnumeratorObject* self = reinterpret_cast<FastEnumeratorObject*>(type->tp_alloc(type, 0));
    if (self) self->impl = nullptr;
    return reinterpret_cast<PyObject*>(self);
}

static int FastEnumerator_init(FastEnumeratorObject* self, PyObject* args, PyObject* kwargs) {
    static const char* kwlist[] = {
        "atom_masks", "bond_masks", "adjacency_masks", "closure_masks",
        "min_atoms", "max_atoms", "shard_index", "shard_count", "root_unit_index",
        "root_bucket_index", "root_bucket_count", "limit_states", "limit_fragments", nullptr
    };
    PyObject* py_atom_masks = nullptr;
    PyObject* py_bond_masks = nullptr;
    PyObject* py_adjacency_masks = nullptr;
    PyObject* py_closure_masks = nullptr;
    int min_atoms = 3;
    int max_atoms = 0;
    int shard_index = 0;
    int shard_count = 1;
    PyObject* py_root_unit_index = Py_None;
    int root_bucket_index = 0;
    int root_bucket_count = 1;
    PyObject* py_limit_states = Py_None;
    PyObject* py_limit_fragments = Py_None;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "OOOOiiiiOiiOO", const_cast<char**>(kwlist),
            &py_atom_masks, &py_bond_masks, &py_adjacency_masks, &py_closure_masks,
            &min_atoms, &max_atoms, &shard_index, &shard_count, &py_root_unit_index,
            &root_bucket_index, &root_bucket_count, &py_limit_states, &py_limit_fragments)) {
        return -1;
    }

    int root_unit_index = -1;
    if (py_root_unit_index != Py_None) {
        root_unit_index = static_cast<int>(PyLong_AsLong(py_root_unit_index));
        if (PyErr_Occurred()) return -1;
    }
    long long limit_states = -1;
    if (py_limit_states != Py_None) {
        limit_states = PyLong_AsLongLong(py_limit_states);
        if (PyErr_Occurred()) return -1;
    }
    long long limit_fragments = -1;
    if (py_limit_fragments != Py_None) {
        limit_fragments = PyLong_AsLongLong(py_limit_fragments);
        if (PyErr_Occurred()) return -1;
    }

    try {
        delete self->impl;
        self->impl = new Enumerator();
        self->impl->initialize(
            py_atom_masks, py_bond_masks, py_adjacency_masks, py_closure_masks,
            min_atoms, max_atoms, shard_index, shard_count, root_unit_index,
            root_bucket_index, root_bucket_count, limit_states, limit_fragments);
    } catch (const std::exception& exc) {
        PyErr_SetString(PyExc_RuntimeError, exc.what());
        return -1;
    }
    return 0;
}

static void FastEnumerator_dealloc(FastEnumeratorObject* self) {
    delete self->impl;
    Py_TYPE(self)->tp_free(reinterpret_cast<PyObject*>(self));
}

static PyObject* FastEnumerator_next_batch(FastEnumeratorObject* self, PyObject* args, PyObject* kwargs) {
    static const char* kwlist[] = {"max_records", nullptr};
    Py_ssize_t max_records = 10000;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "n", const_cast<char**>(kwlist), &max_records)) {
        return nullptr;
    }
    if (!self->impl) {
        PyErr_SetString(PyExc_RuntimeError, "FastEnumerator is not initialized");
        return nullptr;
    }
    if (max_records < 1) max_records = 1;
    try {
        return self->impl->next_batch(static_cast<size_t>(max_records));
    } catch (const std::exception& exc) {
        PyErr_SetString(PyExc_RuntimeError, exc.what());
        return nullptr;
    }
}

static PyMethodDef FastEnumerator_methods[] = {
    {"next_batch", reinterpret_cast<PyCFunction>(FastEnumerator_next_batch), METH_VARARGS | METH_KEYWORDS,
     "Return the next batch of fragment state keys and atom/bond index lists."},
    {nullptr, nullptr, 0, nullptr}
};

static PyTypeObject FastEnumeratorType = {
    PyVarObject_HEAD_INIT(nullptr, 0)
};

static PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "fast_fragment_core",
    "C++ fragment state enumerator for split_smile one-off acceleration.",
    -1,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
    nullptr
};

} // namespace

PyMODINIT_FUNC PyInit_fast_fragment_core(void) {
    FastEnumeratorType.tp_name = "fast_fragment_core.FastEnumerator";
    FastEnumeratorType.tp_basicsize = sizeof(FastEnumeratorObject);
    FastEnumeratorType.tp_itemsize = 0;
    FastEnumeratorType.tp_dealloc = reinterpret_cast<destructor>(FastEnumerator_dealloc);
    FastEnumeratorType.tp_flags = Py_TPFLAGS_DEFAULT;
    FastEnumeratorType.tp_doc = "Streaming C++ fragment state enumerator";
    FastEnumeratorType.tp_methods = FastEnumerator_methods;
    FastEnumeratorType.tp_init = reinterpret_cast<initproc>(FastEnumerator_init);
    FastEnumeratorType.tp_new = FastEnumerator_new;

    if (PyType_Ready(&FastEnumeratorType) < 0) return nullptr;
    PyObject* module = PyModule_Create(&moduledef);
    if (!module) return nullptr;
    Py_INCREF(&FastEnumeratorType);
    if (PyModule_AddObject(module, "FastEnumerator", reinterpret_cast<PyObject*>(&FastEnumeratorType)) < 0) {
        Py_DECREF(&FastEnumeratorType);
        Py_DECREF(module);
        return nullptr;
    }
    return module;
}

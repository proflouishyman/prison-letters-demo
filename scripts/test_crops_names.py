"""Regression test: handwriting OCR misreads of a person's name must still be masked (144627 'Freckerck Maxs')."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import crops

tokens = crops.name_tokens(["Frederick Mason"])
assert crops._word_is_name("Freckerck", tokens), "misread first name not matched"
assert not crops._word_is_name("currently", tokens), "ordinary word matched as a name"
assert not crops._word_is_name("Pandemic", tokens), "ordinary capitalized word matched as a name"
print("PASS: test_crops_names")

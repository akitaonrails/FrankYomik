# Frank Manga

I have a lot of manga e-books bought on Amazon.go.jp that I can read at read.amazon.co.jp.

As someone not formally trained in Japanese, I can mostly read shounen manga (sample at docs/shounen.png). The kanjis have "furigana", which is hiragana based subtitles next to them, so I can read.

But I can barely read adult-orient (not porn, just mature stories) but they mostly have kanjis without furigana (sample at docs/adult.png)

The main goal is to create a Brave/Chromium extension that I can turn on and off, and it can both add furigana next to the kanjis that don't have them, or translate the dialogue straight into english baloons.

Before creating the extension (but having that in mind), I want first to create a proof of concept program (choose the best language depending on availability of libraries that can assist this task).

I am using Arch Linux, so consider having linux tools at your disposal, but also consider that later I will want an Extension version, so it has to work in a browser.

This experiment will create adult-furigana.png and shounen-en.png, one with furigana added next to the kanjis, obeying the font style/size and vertical positioning. the other will have english text in horizontal format but using roughly the same space of the original baloon.

Research and build this proof of concept first.

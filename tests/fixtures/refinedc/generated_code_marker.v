From caesium Require Export notation.
From caesium Require Import tactics.
From refinedc.typing Require Import annotations.
Set Default Proof Using "Type".

(* Generated from [mark.c]. *)
Section code.
  Definition file_0 : string := "mark.c".
  Definition file_1 : string := "/Users/theooltean/.opam/fver/lib/refinedc/include/refinedc.h".
  Definition loc_2 : location_info := LocationInfo file_1 117 2 117 47.
  Definition loc_3 : location_info := LocationInfo file_1 117 9 117 46.
  Definition loc_4 : location_info := LocationInfo file_1 117 9 117 32.
  Definition loc_5 : location_info := LocationInfo file_1 117 33 117 37.
  Definition loc_6 : location_info := LocationInfo file_1 117 33 117 37.
  Definition loc_7 : location_info := LocationInfo file_1 117 39 117 45.
  Definition loc_8 : location_info := LocationInfo file_1 117 39 117 45.
  Definition loc_11 : location_info := LocationInfo file_0 125 26 125 35.
  Definition loc_12 : location_info := LocationInfo file_0 125 33 125 34.
  Definition loc_15 : location_info := LocationInfo file_0 135 2 135 9.
  Definition loc_16 : location_info := LocationInfo file_0 135 2 135 4.
  Definition loc_17 : location_info := LocationInfo file_0 135 3 135 4.
  Definition loc_18 : location_info := LocationInfo file_0 135 3 135 4.
  Definition loc_19 : location_info := LocationInfo file_0 135 7 135 8.

  (* Definition of struct [__cerbty_unnamed_tag_486]. *)
  Program Definition struct___cerbty_unnamed_tag_486 := {|
    sl_members := [
      (Some "__dummy_max_align_t", void*)
    ];
  |}.
  Solve Obligations with solve_struct_obligations.

  (* Definition of function [copy_alloc_id]. *)
  Definition impl_copy_alloc_id : function := {|
    f_args := [
      ("to", it_layout uintptr_t);
      ("from", void*)
    ];
    f_local_vars := [
    ];
    f_init := "#0";
    f_code := (
      <[ "#0" :=
        locinfo: loc_2 ;
        Return (LocInfoE loc_3 (CopyAllocId (IntOp uintptr_t) (LocInfoE loc_5 (use{IntOp uintptr_t} (LocInfoE loc_6 ("to")))) (LocInfoE loc_7 (use{PtrOp} (LocInfoE loc_8 ("from"))))))
      ]> $∅
    )%E
  |}.

  (* Definition of function [__fver_marker]. *)
  Definition impl___fver_marker : function := {|
    f_args := [
    ];
    f_local_vars := [
    ];
    f_init := "#0";
    f_code := (
      <[ "#0" :=
        locinfo: loc_11 ;
        Return (LocInfoE loc_12 (i2v 0 i32))
      ]> $∅
    )%E
  |}.

  (* Definition of function [set1]. *)
  Definition impl_set1 : function := {|
    f_args := [
      ("p", void*)
    ];
    f_local_vars := [
    ];
    f_init := "#0";
    f_code := (
      <[ "#0" :=
        locinfo: loc_15 ;
        LocInfoE loc_17 (!{PtrOp} (LocInfoE loc_18 ("p"))) <-{ IntOp i32 }
          LocInfoE loc_19 (i2v 1 i32) ;
        Return (VOID)
      ]> $∅
    )%E
  |}.
End code.
